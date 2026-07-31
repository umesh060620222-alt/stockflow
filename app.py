"""Local web UI to run + tune the algo. Zero heavy deps (stdlib http.server).

    python app.py        # then open http://127.0.0.1:8000

POST /api/run  applies the posted params to config at runtime, runs the replay
on the configured data source, and returns {summary, trades, sessions}.
"""
from __future__ import annotations
import json, os, traceback, threading, time, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config, data as D, strategy as S, engine as E, zerodha as Z
import recommend as REC
import newsflash as NF
from live import ENGINE as LIVE
from options_autotrader import OptionsAutoTrader
options_trader = OptionsAutoTrader()

_REC_CACHE = {}   # market -> {"date": ..., "data": ...}
_BRACKET = {}     # key (symbol) -> bracket state, see _place_bracket_after_fill
_TICK_CACHE = {}  # (exchange, tradingsymbol) -> tick_size


def _get_tick_size(kc, exchange, tradingsymbol):
    """Tick size varies per instrument (0.05, 0.10, ...) — fetch the real value
    instead of assuming, since Zerodha rejects any price that isn't an exact
    multiple of it (confirmed: TATACONSUM is 0.10, not the usual 0.05)."""
    key = (exchange, tradingsymbol)
    if key in _TICK_CACHE:
        return _TICK_CACHE[key]
    tick = 0.05
    try:
        instruments = kc.instruments(exchange)
        m = next((i for i in instruments if i.get("tradingsymbol") == tradingsymbol), None)
        if m and m.get("tick_size"):
            tick = float(m["tick_size"])
    except Exception as e:
        print(f"[trading] tick_size lookup failed for {tradingsymbol}, defaulting to 0.05: {e}", flush=True)
    _TICK_CACHE[key] = tick
    return tick


def _round_tick(price, tick=0.05):
    """Snap a price to the exchange's tick-size grid. Naive percentage-based prices
    (e.g. entry*0.995) almost never land on a valid multiple, and Zerodha rejects
    orders that don't."""
    return round(round(price / tick) * tick, 2)


def _resolve_option(kc, symbol, ltp, opt_type):
    """Nearest-expiry NFO instrument for symbol/type, at whichever listed strike is
    closest to ltp — real listed strikes, not a guessed interval (strike spacing
    varies per stock). Returns {token, tradingsymbol, expiry, strike, lot_size} or None."""
    instruments = kc.instruments("NFO")
    today = datetime.date.today()
    candidates = [i for i in instruments
                  if i.get("name") == symbol
                  and i.get("instrument_type") == opt_type
                  and i.get("expiry") and i["expiry"] >= today]
    if not candidates:
        return None
    nearest_expiry = min(i["expiry"] for i in candidates)
    same_expiry = [i for i in candidates if i["expiry"] == nearest_expiry]
    m = min(same_expiry, key=lambda i: abs(float(i["strike"] or 0) - ltp))
    return {
        "token":         m["instrument_token"],
        "tradingsymbol": m["tradingsymbol"],
        "expiry":        str(m["expiry"]),
        "strike":        float(m["strike"]),
        "lot_size":      int(m.get("lot_size") or 500),
    }


def _place_bracket(kc, key, exchange, tradingsymbol, exit_side, quantity, product,
                    sl_trigger, target_price, entry_price=None):
    """Rest an SL (stop-loss limit) + a LIMIT target on the exchange for an already-
    filled position (a manual OCO bracket) and start polling for either to fill.
    Uses SL (limit), not SL-M (market) — SL-M requires "market protection" that
    isn't enabled for API orders on this account and gets rejected outright."""
    tick = _get_tick_size(kc, exchange, tradingsymbol)
    sl_trigger = _round_tick(sl_trigger, tick)
    target_price = _round_tick(target_price, tick)
    tt = kc.TRANSACTION_TYPE_SELL if exit_side == "SELL" else kc.TRANSACTION_TYPE_BUY
    # SL-limit needs a limit price past the trigger so it's still marketable once hit
    sl_limit = _round_tick(sl_trigger * (1.005 if exit_side == "BUY" else 0.995), tick)
    sl_oid = kc.place_order(
        variety=kc.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol,
        transaction_type=tt, quantity=quantity, product=product,
        order_type=kc.ORDER_TYPE_SL, trigger_price=sl_trigger, price=sl_limit,
    )
    target_oid = kc.place_order(
        variety=kc.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol,
        transaction_type=tt, quantity=quantity, product=product,
        order_type=kc.ORDER_TYPE_LIMIT, price=target_price,
    )
    _BRACKET[key] = {
        "sl_id": sl_oid, "target_id": target_oid, "exchange": exchange,
        "tradingsymbol": tradingsymbol, "product": product, "quantity": quantity,
        "exit_side": exit_side, "entry_price": entry_price,
        "closed": False, "result": None, "exit_price": None,
    }
    print(f"[trading] {tradingsymbol} bracket armed → "
          f"SL {sl_oid} trigger={sl_trigger}/limit={sl_limit} / target {target_oid}@{target_price}", flush=True)
    threading.Thread(target=_poll_bracket, args=(kc, key), daemon=True).start()
    return _BRACKET[key]


def _poll_bracket(kc, key):
    """Poll a resting bracket until one side fills, cancelling the other, or force
    square off past 15:20 IST if still open."""
    for _ in range(7200):   # up to 4 hours (7200 × 2s)
        time.sleep(2)
        b = _BRACKET.get(key)
        if not b or b["closed"]:
            return
        try:
            orders = kc.orders()
            sl_o = next((x for x in orders if str(x["order_id"]) == str(b["sl_id"])), None)
            tg_o = next((x for x in orders if str(x["order_id"]) == str(b["target_id"])), None)
            if sl_o and sl_o["status"] == "COMPLETE":
                _try_cancel(kc, b["target_id"])
                b.update(closed=True, result="SL", exit_price=sl_o.get("average_price"))
                return
            if tg_o and tg_o["status"] == "COMPLETE":
                _try_cancel(kc, b["sl_id"])
                b.update(closed=True, result="TARGET", exit_price=tg_o.get("average_price"))
                return
            now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            if now_ist.hour * 60 + now_ist.minute >= 15 * 60 + 20:   # >= 15:20 IST
                _force_square_off(kc, key, "EOD")
                return
        except Exception as e:
            print(f"[trading] bracket poll error {key}: {e}", flush=True)


def _place_bracket_after_fill(kc, entry_oid, key, exchange, tradingsymbol, exit_side,
                               quantity, product, sl_trigger, target_price):
    """Poll (read-only status checks) until the entry order fills, then arm the exit
    bracket exactly once — if that fails, stop and report instead of retrying on
    every subsequent poll tick (a prior version did that and hammered the API with
    ~160 rejected orders in one incident)."""
    for _ in range(240):          # up to 2 min (240 × 0.5s) waiting for entry fill
        time.sleep(0.5)
        try:
            orders = kc.orders()
            o = next((x for x in orders if str(x["order_id"]) == str(entry_oid)), None)
        except Exception as e:
            print(f"[trading] poll error {tradingsymbol}: {e}", flush=True)
            continue
        if not o:
            continue
        if o["status"] == "COMPLETE":
            filled_qty = int(o.get("filled_quantity") or o.get("quantity") or quantity)
            try:
                _place_bracket(kc, key, exchange, tradingsymbol, exit_side, filled_qty,
                                product, sl_trigger, target_price, entry_price=o.get("average_price"))
            except Exception as e:
                print(f"[trading] FAILED to place bracket for {tradingsymbol} — "
                      f"position is UNPROTECTED, not retrying: {e}", flush=True)
            return
        if o["status"] in ("CANCELLED", "REJECTED"):
            print(f"[trading] {tradingsymbol} entry {o['status']} — no bracket placed", flush=True)
            return
    print(f"[trading] timed out waiting for fill on {entry_oid}", flush=True)


def _try_cancel(kc, order_id):
    try:
        kc.cancel_order(variety=kc.VARIETY_REGULAR, order_id=str(order_id))
    except Exception as e:
        print(f"[trading] cancel {order_id} failed (likely already filled/cancelled): {e}", flush=True)


def _force_square_off(kc, key, result):
    b = _BRACKET.get(key)
    if not b or b["closed"]:
        return
    if b.get("sl_id"):
        _try_cancel(kc, b["sl_id"])
    if b.get("target_id"):
        _try_cancel(kc, b["target_id"])
    try:
        tt = kc.TRANSACTION_TYPE_SELL if b["exit_side"] == "SELL" else kc.TRANSACTION_TYPE_BUY
        oid = kc.place_order(
            variety=kc.VARIETY_REGULAR, exchange=b["exchange"], tradingsymbol=b["tradingsymbol"],
            transaction_type=tt, quantity=b["quantity"], product=b["product"],
            order_type=kc.ORDER_TYPE_MARKET,
        )
        b.update(closed=True, result=result, exit_price=None, square_off_order_id=oid)
    except Exception as e:
        print(f"[trading] force square-off failed {key}: {e}", flush=True)

HERE = os.path.dirname(__file__)

# params the UI may override -> (config attr, cast)
PARAMS = {
    "source": ("SOURCE", str), "interval": ("INTERVAL", str), "period": ("PERIOD", str),
    "mode": ("MODE", str), "deviation_pct": ("DEVIATION_PCT", float),
    "vol_mult": ("VOL_MULT", float), "mom_lookback": ("MOM_LOOKBACK", int),
    "target_pct": ("TARGET_PCT", float), "stop_pct": ("STOP_PCT", float),
    "time_stop_min": ("TIME_STOP_MIN", int), "max_positions": ("MAX_POSITIONS", int),
    "cost_pct": ("COST_PCT", float), "slippage_pct": ("SLIPPAGE_PCT", float),
    "atr_drop_mult": ("ATR_DROP_MULT", float),
    "atr_bounce_mult": ("ATR_BOUNCE_MULT", float),
    "use_nifty_filter": ("USE_NIFTY_FILTER", lambda x: x is True or str(x).lower() in ("true", "1", "yes", "on")),
}


def _jsonable(o):
    """Coerce numpy/pandas scalars that json doesn't natively handle."""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def dumps(o):
    return json.dumps(o, default=_jsonable)


def defaults():
    return {k: getattr(config, attr) for k, (attr, _) in PARAMS.items()}


def run_algo(overrides: dict) -> dict:
    for k, (attr, cast) in PARAMS.items():
        if k in overrides and overrides[k] not in (None, ""):
            setattr(config, attr, cast(overrides[k]))

    raw = D.fetch()
    bench = raw.get(config.BENCHMARK)
    bench_ret = None
    if bench is not None and not bench.empty:
        bench_ret = bench.groupby("date")["close"].transform(lambda s: s / s.iloc[0] - 1.0)

    prepared = {}
    for sym, df in raw.items():
        if sym == config.BENCHMARK or df is None or df.empty:
            continue
        prepared[sym] = S.add_indicators(df, bench_ret)
    if not prepared:
        return {"error": "No data fetched. Check the data source / login."}

    sessions = sorted({str(d) for df in prepared.values() for d in df["date"].unique()})
    result = E.run(prepared)
    # make trades JSON-safe (timestamps -> strings)
    trades = []
    for t in result["trades"]:
        t = dict(t)
        t["entry_ts"] = t["entry_ts"].strftime("%Y-%m-%d %H:%M")
        t["exit_ts"] = t["exit_ts"].strftime("%H:%M")
        trades.append(t)
    return {"summary": result["summary"], "trades": trades,
            "sessions": sessions, "n_symbols": len(prepared), "params": defaults()}


def run_options_algo(overrides: dict) -> dict:
    import math
    import pandas as pd
    import yfinance as yf
    # Retrieve options params
    capital = float(overrides.get("capital", 40000.0))
    lot_size = 75
    delta = 0.50
    target_pct = 0.0030
    stop_pct = 0.0015
    period = overrides.get("period", "7d")
    sl_atr_mult = float(overrides.get("sl_atr_mult", 1.0))
    target_atr_mult = float(overrides.get("target_atr_mult", 2.0))
    trail_halfway_mult = float(overrides.get("trail_halfway_mult", 0.5))
    raw_max_dur = overrides.get("max_duration_mins", 45)
    if raw_max_dur in (None, "None", 0, "0"):
        max_duration_mins = None
    else:
        max_duration_mins = int(raw_max_dur)
    lot_size_mode = overrides.get("lot_size_mode", "auto").strip().lower()
    fixed_lots = int(overrides.get("fixed_lots", 1))
    
    # Download Nifty spot data
    raw = pd.DataFrame()
    source = overrides.get("source", config.SOURCE)
    
    if source == "zerodha":
        try:
            import zerodha
            z_res = zerodha.fetch(["^NSEI"], interval="1m", period=period)
            if "^NSEI" in z_res and not z_res["^NSEI"].empty:
                raw = z_res["^NSEI"]
                print("Successfully loaded Nifty Spot index from Zerodha Connect!")
        except Exception as e:
            print(f"Zerodha fetch failed: {e}. Trying yfinance...")
            
    if raw.empty:
        try:
            raw = yf.download("^NSEI", period=period, interval="1m", progress=False)
        except Exception as e:
            print(f"yfinance download failed: {e}")
            
    if raw.empty:
        if source != "zerodha":
            try:
                import zerodha
                z_res = zerodha.fetch(["^NSEI"], interval="1m", period=period)
                if "^NSEI" in z_res and not z_res["^NSEI"].empty:
                    raw = z_res["^NSEI"]
                    print("Successfully loaded Nifty Spot index from Zerodha Connect fallback!")
            except Exception as e:
                print(f"Zerodha fallback fetch failed: {e}")
                
    if raw.empty:
        return {"error": "Failed to retrieve Nifty 50 data from both Yahoo Finance and Zerodha."}
    
    df = raw.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Kolkata")
    
    # Fetch Nifty Futures volume data for the backtest period
    fut_vol_map = {}
    try:
        import zerodha
        import datetime as dt
        kc = zerodha.kite()
        nfo = zerodha.get_nfo_instruments(kc)
        nifty_futs = [i for i in nfo if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"]
        if nifty_futs:
            nifty_futs = sorted(nifty_futs, key=lambda x: x.get("expiry"))
            fut_tok = int(nifty_futs[0]["instrument_token"])
            min_ts = df.index.min()
            max_ts = df.index.max()
            from_d = min_ts.tz_convert("UTC").replace(tzinfo=None)
            to_d = max_ts.tz_convert("UTC").replace(tzinfo=None)
            fut_rows = kc.historical_data(fut_tok, from_d, to_d, "minute")
            for r in fut_rows:
                r_ts = r["date"]
                if r_ts.tzinfo is None:
                    r_ts = r_ts.replace(tzinfo=dt.timezone.utc)
                r_ts_ist = r_ts.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
                fut_vol_map[r_ts_ist] = float(r["volume"])
            print(f"Successfully loaded Nifty Futures volume data: {len(fut_vol_map)} rows mapped.")
    except Exception as e:
        print(f"Failed to fetch Nifty Futures volume: {e}. Volume filter will be ignored.")
        
    dates = sorted(list(set(df.index.date)))
    
    daily_summaries = []
    trades = []
    
    def get_trade_expiry_and_decay(dt_val, t_str):
        try:
            import zerodha
            kc = zerodha.kite()
            exp_date = zerodha.get_expiry_date(kc, dt_val)
        except Exception:
            days_until_tuesday = (1 - dt_val.weekday() + 7) % 7
            exp_date = dt_val + datetime.timedelta(days=days_until_tuesday)
            
        if dt_val == exp_date:
            try:
                h, m = map(int, t_str.split(":"))
                if h > 12 or (h == 12 and m >= 30):
                    next_exp = None
                    try:
                        import zerodha
                        kc = zerodha.kite()
                        insts = zerodha.get_nfo_instruments(kc)
                        if insts:
                            expiries = sorted(list({
                                datetime.datetime.strptime(i["expiry"], "%Y-%m-%d").date()
                                for i in insts
                                if i.get("name") == "NIFTY" and i.get("expiry")
                            }))
                            future_exp = [e for e in expiries if e > dt_val]
                            if future_exp:
                                next_exp = future_exp[0]
                    except Exception:
                        pass
                    exp_date = next_exp or (dt_val + datetime.timedelta(days=7))
            except Exception:
                pass
        days_to_exp = (exp_date - dt_val).days
        dec_fac = 0.005 if days_to_exp <= 1 else 0.007 if days_to_exp == 2 else 0.010 if days_to_exp == 3 else 0.013 if days_to_exp <= 5 else 0.016
        return exp_date, dec_fac

    for d in dates:
        
        df_session = df[df.index.date == d].copy()
        if isinstance(df_session.columns, pd.MultiIndex):
            df_session.columns = [col[0].lower() for col in df_session.columns]
        else:
            df_session.columns = df_session.columns.str.lower()
        df_session = df_session[["open", "high", "low", "close"]]
        df_session = df_session.dropna(how="any")
        if df_session.empty:
            continue
            
        df_session["date"] = df_session.index
        candles = df_session.to_dict("records")
        nifty_open = float(df_session["open"].iloc[0])
        
        # Map futures volume and compute 10-period volume SMA
        vol_history = []
        for c in candles:
            c_ts = c["date"]
            c["volume"] = fut_vol_map.get(c_ts, 0.0)
            vol_history.append(c["volume"])
            if len(vol_history) > 10:
                vol_history.pop(0)
            c["vol_sma"] = sum(vol_history) / len(vol_history) if len(vol_history) >= 5 else 0.0
        
        # Calculate Indicators
        prev_close = None
        tr_history = []
        for idx, c in enumerate(candles):
            high = float(c["high"])
            low = float(c["low"])
            close = float(c["close"])
            if prev_close is None:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            prev_close = close
            tr_history.append(tr)
            if idx < 13:
                c["atr"] = sum(tr_history) / len(tr_history)
            elif idx == 13:
                c["atr"] = sum(tr_history) / 14.0
            else:
                prev_atr = candles[idx-1]["atr"]
                c["atr"] = (prev_atr * 13.0 + tr) / 14.0
            
        closes_s = pd.Series([float(c["close"]) for c in candles])
        ema_series = closes_s.ewm(span=15, adjust=False).mean().tolist()
        for idx, c in enumerate(candles):
            c["nifty_ema"] = ema_series[idx]
            
        # Run state machine
        l_peak = None
        l_trough = None
        l_peak_atr = None
        l_stage = 1
        
        s_trough = None
        s_peak = None
        s_trough_atr = None
        s_stage = 1
        
        locked_until_idx = -1
        session_trades = []
        
        for i, c in enumerate(candles):
            high = float(c["high"])
            low = float(c["low"])
            close = float(c["close"])
            atr = float(c["atr"])
            ts = c["date"]
            nifty_ema = float(c["nifty_ema"])
            
            if i <= locked_until_idx:
                continue
                
            # Volume confirmation check (last completed candle's volume > 10 SMA)
            has_vol_conf = True
            if i >= 1:
                prev_c = candles[i-1]
                prev_vol = prev_c.get("volume", 0.0)
                prev_sma = prev_c.get("vol_sma", 0.0)
                if prev_vol > 0 and prev_sma > 0:
                    has_vol_conf = (prev_vol > prev_sma)
                    
            is_nifty_above_ema = close > nifty_ema
            is_nifty_below_ema = close < nifty_ema
            is_nifty_green_today = close > nifty_open
            is_nifty_red_today = close < nifty_open
            
            time_str = ts.strftime("%H:%M")
            is_valid_time = "09:25" <= time_str < "15:30"
            
            # LONG SETUP
            long_triggered = False
            if l_stage == 1:
                if l_peak is None or high > l_peak:
                    l_peak = high
                    l_peak_atr = atr
                else:
                    l_trough = low
                    l_stage = 2
            elif l_stage == 2:
                if high > l_peak:
                    l_peak = high
                    l_peak_atr = atr
                    l_trough = low
                    l_stage = 1
                else:
                    l_trough = min(l_trough, low)
                    drop_required = 2.5 * (l_peak_atr if l_peak_atr else atr)
                    if l_trough <= l_peak - drop_required:
                        l_stage = 3
            elif l_stage == 3:
                if low < l_trough:
                    l_trough = low
                bounce_required = 0.7 * atr
                bounce_level = l_trough + bounce_required
                if high >= bounce_level:
                    if is_valid_time and is_nifty_above_ema and is_nifty_green_today and has_vol_conf:
                        entry = bounce_level
                        sl_points = max(sl_atr_mult * atr, 7.0)
                        target_points = max(target_atr_mult * atr, 14.0)
                        
                        sl = entry - sl_points
                        target = entry + target_points
                        
                        trade_result = "OPEN"
                        exit_price_val = None
                        exit_time = "-"
                        duration = 0
                        
                        if low <= sl:
                            trade_result = "LOSS"
                            exit_price_val = sl
                            locked_until_idx = i
                            exit_time = time_str
                        elif high >= target:
                            trade_result = "WIN"
                            exit_price_val = target
                            locked_until_idx = i
                            exit_time = time_str
                        else:
                            reached_halfway = False
                            current_sl = sl
                            for idx_w, w in enumerate(candles[i+1:], start=i+1):
                                w_low = float(w["low"])
                                w_high = float(w["high"])
                                w_close = float(w["close"])
                                
                                # halfway_level = entry + trail_halfway_mult * (target - entry)
                                # if w_high >= halfway_level:
                                #     reached_halfway = True
                                #     current_sl = entry
                                if w_low <= sl:
                                    trade_result = "LOSS"
                                    exit_price_val = sl
                                    locked_until_idx = idx_w
                                    exit_time = w["date"].strftime("%H:%M")
                                    duration = int((w["date"] - ts).total_seconds() / 60)
                                    break
                                if w_high >= target:
                                    trade_result = "WIN"
                                    exit_price_val = target
                                    locked_until_idx = idx_w
                                    exit_time = w["date"].strftime("%H:%M")
                                    duration = int((w["date"] - ts).total_seconds() / 60)
                                    break
                                if max_duration_mins is not None and (idx_w - i) >= max_duration_mins:
                                    trade_result = "TIMEOUT"
                                    exit_price_val = w_close
                                    locked_until_idx = idx_w
                                    exit_time = w["date"].strftime("%H:%M")
                                    duration = int((w["date"] - ts).total_seconds() / 60)
                                    break
                                    
                        strike_rounded = int(round(entry / 50.0) * 50.0)
                        trade_expiry, decay_factor = get_trade_expiry_and_decay(d, time_str)
                        expiry_str = trade_expiry.strftime("%d %b").upper()
                        
                        # Attempt to download actual option candles from Zerodha
                        opt_candles = {}
                        try:
                            import zerodha
                            import pytz
                            kc = zerodha.kite()
                            opt_token = zerodha.get_option_token(kc, "NIFTY", trade_expiry, strike_rounded, "CE")
                            if opt_token:
                                opt_from = datetime.datetime.combine(d, datetime.time(9, 15))
                                opt_to = datetime.datetime.combine(d, datetime.time(15, 30))
                                rows = kc.historical_data(opt_token, opt_from, opt_to, "minute")
                                for r in rows:
                                    ts_row = r["date"].astimezone(pytz.timezone("Asia/Kolkata")) if r["date"].tzinfo else r["date"]
                                    time_key = ts_row.strftime("%H:%M")
                                    opt_candles[time_key] = {
                                        "open": float(r["open"]),
                                        "high": float(r["high"]),
                                        "low": float(r["low"]),
                                        "close": float(r["close"])
                                    }
                        except Exception as e:
                            print(f"Failed to fetch actual Nifty CE option candles: {e}")
                            
                        def get_opt_price(t_str, default_val, field="close"):
                            if not opt_candles:
                                return default_val
                            if t_str in opt_candles:
                                return opt_candles[t_str][field]
                            try:
                                h, m = map(int, t_str.split(":"))
                                dt_curr = datetime.datetime(2000, 1, 1, h, m)
                                for offset in range(1, 11):
                                    dt_next = dt_curr + datetime.timedelta(minutes=offset)
                                    next_t_str = dt_next.strftime("%H:%M")
                                    if next_t_str in opt_candles:
                                        return opt_candles[next_t_str][field]
                            except Exception:
                                pass
                            return default_val

                        # Estimate fallback premium
                        fallback_premium = entry * decay_factor
                        premium = get_opt_price(time_str, fallback_premium, field="close")
                        
                        if lot_size_mode == "fixed":
                            lots = fixed_lots
                        else:
                            lot_cost = premium * lot_size
                            lots = math.floor(capital / lot_cost) if lot_cost > 0 else 0
                        total_shares = lots * lot_size
                        
                        options_brokerage = 40.0
                        options_slippage = 0.0
                        
                        # Set actual/fallback exit premium and PnL
                        exit_premium = None
                        if trade_result in ("WIN", "LOSS", "BREAKEVEN", "TIMEOUT"):
                            spot_change = exit_price_val - entry
                            d_delta = 0.55 if trade_result == "WIN" else 0.45
                            fallback_exit = premium + (spot_change * d_delta)
                            
                            field = "high" if trade_result == "WIN" else ("low" if trade_result == "LOSS" else "close")
                            exit_premium = get_opt_price(exit_time, fallback_exit, field=field)
                            pnl_gross = (exit_premium - premium) * total_shares
                            pnl_net = pnl_gross - options_brokerage - (options_slippage * total_shares)
                        else:
                            pnl_net = 0.0
                            
                        symbol_str = f"NIFTY {expiry_str} {strike_rounded} CE"
                        
                        session_trades.append({
                            "date": str(d),
                            "side": "BUY CALL (CE)",
                            "symbol": symbol_str,
                            "entry_time": time_str,
                            "exit_time": exit_time,
                            "duration": f"{duration}m" if trade_result != "OPEN" else "-",
                            "entry_spot": entry,
                            "exit_spot": exit_price_val,
                            "entry_premium": premium,
                            "exit_premium": exit_premium,
                            "result": trade_result,
                            "pnl": pnl_net,
                            "lots": lots
                        })
                        long_triggered = True
                        l_peak = None
                        l_trough = None
                        l_peak_atr = None
                        l_stage = 1
                        
            # SHORT SETUP
            if not long_triggered:
                if s_stage == 1:
                    if s_trough is None or low < s_trough:
                        s_trough = low
                        s_trough_atr = atr
                    else:
                        s_peak = high
                        s_stage = 2
                elif s_stage == 2:
                    if low < s_trough:
                        s_trough = low
                        s_trough_atr = atr
                        s_peak = high
                        s_stage = 1
                    else:
                        s_peak = max(s_peak, high)
                        rally_required = 2.5 * (s_trough_atr if s_trough_atr else atr)
                        if s_peak >= s_trough + rally_required:
                            s_stage = 3
                elif s_stage == 3:
                    if high > s_peak:
                        s_peak = high
                    drop_required = 0.7 * atr
                    short_trigger_level = s_peak - drop_required
                    if low <= short_trigger_level:
                        if is_valid_time and is_nifty_below_ema and is_nifty_red_today and has_vol_conf:
                            entry = short_trigger_level
                            sl_points = max(sl_atr_mult * atr, 7.0)
                            target_points = max(target_atr_mult * atr, 14.0)
                            
                            sl = entry + sl_points
                            target = entry - target_points
                            
                            trade_result = "OPEN"
                            exit_price_val = None
                            exit_time = "-"
                            duration = 0
                            
                            if high >= sl:
                                trade_result = "LOSS"
                                exit_price_val = sl
                                locked_until_idx = i
                                exit_time = time_str
                            elif low <= target:
                                trade_result = "WIN"
                                exit_price_val = target
                                locked_until_idx = i
                                exit_time = time_str
                            else:
                                reached_halfway = False
                                current_sl = sl
                                for idx_w, w in enumerate(candles[i+1:], start=i+1):
                                    w_low = float(w["low"])
                                    w_high = float(w["high"])
                                    w_close = float(w["close"])
                                    
                                    # halfway_level = entry - trail_halfway_mult * (entry - target)
                                    # if w_low <= halfway_level:
                                    #     reached_halfway = True
                                    #     current_sl = entry
                                    if w_high >= sl:
                                        trade_result = "LOSS"
                                        exit_price_val = sl
                                        locked_until_idx = idx_w
                                        exit_time = w["date"].strftime("%H:%M")
                                        duration = int((w["date"] - ts).total_seconds() / 60)
                                        break
                                    if w_low <= target:
                                        trade_result = "WIN"
                                        exit_price_val = target
                                        locked_until_idx = idx_w
                                        exit_time = w["date"].strftime("%H:%M")
                                        duration = int((w["date"] - ts).total_seconds() / 60)
                                        break
                                    if max_duration_mins is not None and (idx_w - i) >= max_duration_mins:
                                        trade_result = "TIMEOUT"
                                        exit_price_val = w_close
                                        locked_until_idx = idx_w
                                        exit_time = w["date"].strftime("%H:%M")
                                        duration = int((w["date"] - ts).total_seconds() / 60)
                                        break
                                        
                            strike_rounded = int(round(entry / 50.0) * 50.0)
                            trade_expiry, decay_factor = get_trade_expiry_and_decay(d, time_str)
                            expiry_str = trade_expiry.strftime("%d %b").upper()
                            
                            # Attempt to download actual option candles from Zerodha
                            opt_candles = {}
                            try:
                                import zerodha
                                import pytz
                                kc = zerodha.kite()
                                opt_token = zerodha.get_option_token(kc, "NIFTY", trade_expiry, strike_rounded, "PE")
                                if opt_token:
                                    opt_from = datetime.datetime.combine(d, datetime.time(9, 15))
                                    opt_to = datetime.datetime.combine(d, datetime.time(15, 30))
                                    rows = kc.historical_data(opt_token, opt_from, opt_to, "minute")
                                    for r in rows:
                                        ts_row = r["date"].astimezone(pytz.timezone("Asia/Kolkata")) if r["date"].tzinfo else r["date"]
                                        time_key = ts_row.strftime("%H:%M")
                                        opt_candles[time_key] = {
                                            "open": float(r["open"]),
                                            "high": float(r["high"]),
                                            "low": float(r["low"]),
                                            "close": float(r["close"])
                                        }
                            except Exception as e:
                                print(f"Failed to fetch actual Nifty PE option candles: {e}")
                                
                            def get_opt_price(t_str, default_val, field="close"):
                                if not opt_candles:
                                    return default_val
                                if t_str in opt_candles:
                                    return opt_candles[t_str][field]
                                try:
                                    h, m = map(int, t_str.split(":"))
                                    dt_curr = datetime.datetime(2000, 1, 1, h, m)
                                    for offset in range(1, 11):
                                        dt_next = dt_curr + datetime.timedelta(minutes=offset)
                                        next_t_str = dt_next.strftime("%H:%M")
                                        if next_t_str in opt_candles:
                                            return opt_candles[next_t_str][field]
                                except Exception:
                                    pass
                                return default_val

                            # Estimate fallback premium
                            fallback_premium = entry * decay_factor
                            premium = get_opt_price(time_str, fallback_premium, field="close")
                            
                            if lot_size_mode == "fixed":
                                lots = fixed_lots
                            else:
                                lot_cost = premium * lot_size
                                lots = math.floor(capital / lot_cost) if lot_cost > 0 else 0
                            total_shares = lots * lot_size
                            
                            options_brokerage = 40.0
                            options_slippage = 0.0
                            
                            # Set actual/fallback exit premium and PnL
                            exit_premium = None
                            if trade_result in ("WIN", "LOSS", "BREAKEVEN", "TIMEOUT"):
                                spot_change = entry - exit_price_val
                                d_delta = 0.55 if trade_result == "WIN" else 0.45
                                fallback_exit = premium + (spot_change * d_delta)
                                
                                field = "high" if trade_result == "WIN" else ("low" if trade_result == "LOSS" else "close")
                                exit_premium = get_opt_price(exit_time, fallback_exit, field=field)
                                pnl_gross = (exit_premium - premium) * total_shares
                                pnl_net = pnl_gross - options_brokerage - (options_slippage * total_shares)
                            else:
                                pnl_net = 0.0
                                
                            symbol_str = f"NIFTY {expiry_str} {strike_rounded} PE"
                            
                            session_trades.append({
                                "date": str(d),
                                "side": "BUY PUT (PE)",
                                "symbol": symbol_str,
                                "entry_time": time_str,
                                "exit_time": exit_time,
                                "duration": f"{duration}m" if trade_result != "OPEN" else "-",
                                "entry_spot": entry,
                                "exit_spot": exit_price_val,
                                "entry_premium": premium,
                                "exit_premium": exit_premium,
                                "result": trade_result,
                                "pnl": pnl_net,
                                "lots": lots
                            })
                            s_trough = None
                            s_peak = None
                            s_trough_atr = None
                            s_stage = 1
                            
        # Compute daily stats
        wins = sum(1 for t in session_trades if t["result"] == "WIN")
        losses = sum(1 for t in session_trades if t["result"] == "LOSS")
        be = sum(1 for t in session_trades if t["result"] == "BREAKEVEN")
        opens = sum(1 for t in session_trades if t["result"] == "OPEN")
        total_pnl = sum(t["pnl"] for t in session_trades)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        
        daily_summaries.append({
            "date": str(d),
            "trades": len(session_trades),
            "wins": wins,
            "losses": losses,
            "be": be,
            "open": opens,
            "win_rate": round(win_rate, 1),
            "pnl": round(total_pnl)
        })
        trades.extend(session_trades)
        
    # Compute overall summary
    total_wins = sum(s["wins"] for s in daily_summaries)
    total_losses = sum(s["losses"] for s in daily_summaries)
    total_be = sum(s["be"] for s in daily_summaries)
    total_pnl_all = sum(s["pnl"] for s in daily_summaries)
    overall_win_rate = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0.0
    
    summary = {
        "total_trades": sum(s["trades"] for s in daily_summaries),
        "wins": total_wins,
        "losses": total_losses,
        "be": total_be,
        "win_rate": round(overall_win_rate, 1),
        "total_pnl": round(total_pnl_all)
    }
    
    return {
        "summary": summary,
        "daily": daily_summaries,
        "trades": trades
    }


def run_futures_algo(overrides: dict) -> dict:
    import math
    import pandas as pd
    import yfinance as yf
    
    capital = float(overrides.get("capital", 40000.0))
    lot_size = 75
    period = overrides.get("period", "7d")
    sl_atr_mult = float(overrides.get("sl_atr_mult", 1.0))
    target_atr_mult = float(overrides.get("target_atr_mult", 2.0))
    raw_max_dur = overrides.get("max_duration_mins", 45)
    if raw_max_dur in (None, "None", 0, "0"):
        max_duration_mins = None
    else:
        max_duration_mins = int(raw_max_dur)
    lot_size_mode = overrides.get("lot_size_mode", "auto").strip().lower()
    fixed_lots = int(overrides.get("fixed_lots", 1))
    
    # Download Nifty spot data
    raw = pd.DataFrame()
    source = overrides.get("source", config.SOURCE)
    
    if source == "zerodha":
        try:
            import zerodha
            z_res = zerodha.fetch(["^NSEI"], interval="1m", period=period)
            if "^NSEI" in z_res and not z_res["^NSEI"].empty:
                raw = z_res["^NSEI"]
        except Exception as e:
            print(f"Zerodha fetch failed: {e}. Trying yfinance...")
            
    if raw.empty:
        try:
            raw = yf.download("^NSEI", period=period, interval="1m", progress=False)
        except Exception as e:
            print(f"yfinance download failed: {e}")
            
    if raw.empty:
        if source != "zerodha":
            try:
                import zerodha
                z_res = zerodha.fetch(["^NSEI"], interval="1m", period=period)
                if "^NSEI" in z_res and not z_res["^NSEI"].empty:
                    raw = z_res["^NSEI"]
            except Exception as e:
                print(f"Zerodha fallback fetch failed: {e}")
                
    if raw.empty:
        return {"error": "Failed to retrieve Nifty 50 data."}
    
    df = raw.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Kolkata")
    
    # Futures volume map
    fut_vol_map = {}
    try:
        import zerodha
        import datetime as dt
        kc = zerodha.kite()
        nfo = zerodha.get_nfo_instruments(kc)
        nifty_futs = [i for i in nfo if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"]
        if nifty_futs:
            nifty_futs = sorted(nifty_futs, key=lambda x: x.get("expiry"))
            fut_tok = int(nifty_futs[0]["instrument_token"])
            min_ts = df.index.min()
            max_ts = df.index.max()
            from_d = min_ts.tz_convert("UTC").replace(tzinfo=None)
            to_d = max_ts.tz_convert("UTC").replace(tzinfo=None)
            fut_rows = kc.historical_data(fut_tok, from_d, to_d, "minute")
            for r in fut_rows:
                r_ts = r["date"]
                if r_ts.tzinfo is None:
                    r_ts = r_ts.replace(tzinfo=dt.timezone.utc)
                r_ts_ist = r_ts.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
                fut_vol_map[r_ts_ist] = float(r["volume"])
    except Exception as e:
        pass
        
    dates = sorted(list(set(df.index.date)))
    
    daily_summaries = []
    trades = []
    
    for d in dates:
        df_session = df[df.index.date == d].copy()
        if isinstance(df_session.columns, pd.MultiIndex):
            df_session.columns = [col[0].lower() for col in df_session.columns]
        else:
            df_session.columns = df_session.columns.str.lower()
        df_session = df_session[["open", "high", "low", "close"]]
        df_session = df_session.dropna(how="any")
        if df_session.empty:
            continue
            
        df_session["date"] = df_session.index
        candles = df_session.to_dict("records")
        nifty_open = float(df_session["open"].iloc[0])
        
        # Volume SMA
        vol_history = []
        for c in candles:
            c_ts = c["date"]
            c["volume"] = fut_vol_map.get(c_ts, 0.0)
            vol_history.append(c["volume"])
            if len(vol_history) > 10:
                vol_history.pop(0)
            c["vol_sma"] = sum(vol_history) / len(vol_history) if len(vol_history) >= 5 else 0.0
            
        # Calculate Indicators (14-period Wilder's RMA ATR + 15 EMA)
        prev_close = None
        tr_history = []
        for idx, c in enumerate(candles):
            high = float(c["high"])
            low = float(c["low"])
            close = float(c["close"])
            if prev_close is None:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            prev_close = close
            tr_history.append(tr)
            if idx < 13:
                c["atr"] = sum(tr_history) / len(tr_history)
            elif idx == 13:
                c["atr"] = sum(tr_history) / 14.0
            else:
                prev_atr = candles[idx-1]["atr"]
                c["atr"] = (prev_atr * 13.0 + tr) / 14.0
                
        closes_s = pd.Series([float(c["close"]) for c in candles])
        ema_series = closes_s.ewm(span=15, adjust=False).mean().tolist()
        for idx, c in enumerate(candles):
            c["nifty_ema"] = ema_series[idx]
            
        # Run state machine
        l_peak = None
        l_trough = None
        l_peak_atr = None
        l_stage = 1
        
        s_trough = None
        s_peak = None
        s_trough_atr = None
        s_stage = 1
        
        locked_until_idx = -1
        session_trades = []
        
        for i, c in enumerate(candles):
            high = float(c["high"])
            low = float(c["low"])
            close = float(c["close"])
            atr = float(c["atr"])
            ts = c["date"]
            time_str = ts.strftime("%H:%M")
            
            if i <= locked_until_idx:
                continue
                
            is_valid_time = "09:25" <= time_str < "15:30"
            is_nifty_above_ema = close > c["nifty_ema"]
            is_nifty_below_ema = close < c["nifty_ema"]
            is_nifty_green_today = close > nifty_open
            is_nifty_red_today = close < nifty_open
            has_vol_conf = c["volume"] > c["vol_sma"] if c["vol_sma"] > 0 else True
            
            # --- LONG SETUP ---
            if l_stage == 1:
                if l_peak is None or high > l_peak:
                    l_peak = high
                    l_peak_atr = atr
                else:
                    l_trough = low
                    l_stage = 2
            elif l_stage == 2:
                if high > l_peak:
                    l_peak = high
                    l_peak_atr = atr
                    l_trough = low
                    l_stage = 1
                else:
                    l_trough = min(l_trough, low)
                    drop_required = 2.5 * (l_peak_atr if l_peak_atr else atr)
                    if l_trough <= l_peak - drop_required:
                        l_stage = 3
            elif l_stage == 3:
                if low < l_trough:
                    l_trough = low
                bounce_required = 0.7 * atr
                bounce_level = l_trough + bounce_required
                if high >= bounce_level:
                    if is_valid_time and is_nifty_above_ema and is_nifty_green_today and has_vol_conf:
                        entry = bounce_level
                        sl_points = max(sl_atr_mult * atr, 7.0)
                        target_points = max(target_atr_mult * atr, 14.0)
                        
                        sl = entry - sl_points
                        target = entry + target_points
                        
                        trade_result = "OPEN"
                        exit_price_val = None
                        exit_time = "-"
                        duration = 0
                        
                        if low <= sl:
                            trade_result = "LOSS"
                            exit_price_val = sl
                            locked_until_idx = i
                            exit_time = time_str
                        elif high >= target:
                            trade_result = "WIN"
                            exit_price_val = target
                            locked_until_idx = i
                            exit_time = time_str
                        else:
                            for idx_w, w in enumerate(candles[i+1:], start=i+1):
                                w_low = float(w["low"])
                                w_high = float(w["high"])
                                w_close = float(w["close"])
                                
                                if w_low <= sl:
                                    trade_result = "LOSS"
                                    exit_price_val = sl
                                    locked_until_idx = idx_w
                                    exit_time = w["date"].strftime("%H:%M")
                                    duration = int((w["date"] - ts).total_seconds() / 60)
                                    break
                                if w_high >= target:
                                    trade_result = "WIN"
                                    exit_price_val = target
                                    locked_until_idx = idx_w
                                    exit_time = w["date"].strftime("%H:%M")
                                    duration = int((w["date"] - ts).total_seconds() / 60)
                                    break
                                if max_duration_mins is not None and (idx_w - i) >= max_duration_mins:
                                    trade_result = "TIMEOUT"
                                    exit_price_val = w_close
                                    locked_until_idx = idx_w
                                    exit_time = w["date"].strftime("%H:%M")
                                    duration = int((w["date"] - ts).total_seconds() / 60)
                                    break
                                    
                        # Size position
                        if lot_size_mode == "fixed":
                            lots = fixed_lots
                        else:
                            lots = math.floor(capital / 100000.0)
                            if lots == 0:
                                lots = 1
                        total_shares = lots * lot_size
                        
                        # Costs: flat ₹40 round-trip brokerage + 0.05 pts slippage per share
                        brokerage = 40.0
                        slippage = 0.05 * total_shares
                        
                        if trade_result in ("WIN", "LOSS", "TIMEOUT"):
                            pnl_gross = (exit_price_val - entry) * total_shares
                            pnl_net = pnl_gross - brokerage - slippage
                        else:
                            pnl_net = 0.0
                            
                        session_trades.append({
                            "date": str(d),
                            "side": "BUY FUTURES",
                            "symbol": "NIFTY FUTURES",
                            "entry_time": time_str,
                            "exit_time": exit_time,
                            "duration": f"{duration}m" if trade_result != "OPEN" else "-",
                            "entry_spot": entry,
                            "exit_spot": exit_price_val,
                            "entry_premium": entry,
                            "exit_premium": exit_price_val,
                            "result": trade_result,
                            "pnl": pnl_net,
                            "lots": lots
                        })
                    
                    l_peak = None
                    l_trough = None
                    l_stage = 1
                    
            # --- SHORT SETUP ---
            if not session_trades or session_trades[-1]["entry_time"] != time_str:
                if s_stage == 1:
                    if s_trough is None or low < s_trough:
                        s_trough = low
                        s_trough_atr = atr
                    else:
                        s_peak = high
                        s_stage = 2
                elif s_stage == 2:
                    if low < s_trough:
                        s_trough = low
                        s_trough_atr = atr
                        s_peak = high
                        s_stage = 1
                    else:
                        s_peak = max(s_peak, high)
                        rally_required = 2.5 * (s_trough_atr if s_trough_atr else atr)
                        if s_peak >= s_trough + rally_required:
                            s_stage = 3
                elif s_stage == 3:
                    if high > s_peak:
                        s_peak = high
                    drop_required = 0.7 * atr
                    short_trigger_level = s_peak - drop_required
                    if low <= short_trigger_level:
                        if is_valid_time and is_nifty_below_ema and is_nifty_red_today and has_vol_conf:
                            entry = short_trigger_level
                            sl_points = max(sl_atr_mult * atr, 7.0)
                            target_points = max(target_atr_mult * atr, 14.0)
                            
                            sl = entry + sl_points
                            target = entry - target_points
                            
                            trade_result = "OPEN"
                            exit_price_val = None
                            exit_time = "-"
                            duration = 0
                            
                            if high >= sl:
                                trade_result = "LOSS"
                                exit_price_val = sl
                                locked_until_idx = i
                                exit_time = time_str
                            elif low <= target:
                                trade_result = "WIN"
                                exit_price_val = target
                                locked_until_idx = i
                                exit_time = time_str
                            else:
                                for idx_w, w in enumerate(candles[i+1:], start=i+1):
                                    w_low = float(w["low"])
                                    w_high = float(w["high"])
                                    w_close = float(w["close"])
                                    
                                    if w_high >= sl:
                                        trade_result = "LOSS"
                                        exit_price_val = sl
                                        locked_until_idx = idx_w
                                        exit_time = w["date"].strftime("%H:%M")
                                        duration = int((w["date"] - ts).total_seconds() / 60)
                                        break
                                    if w_low <= target:
                                        trade_result = "WIN"
                                        exit_price_val = target
                                        locked_until_idx = idx_w
                                        exit_time = w["date"].strftime("%H:%M")
                                        duration = int((w["date"] - ts).total_seconds() / 60)
                                        break
                                    if max_duration_mins is not None and (idx_w - i) >= max_duration_mins:
                                        trade_result = "TIMEOUT"
                                        exit_price_val = w_close
                                        locked_until_idx = idx_w
                                        exit_time = w["date"].strftime("%H:%M")
                                        duration = int((w["date"] - ts).total_seconds() / 60)
                                        break
                                        
                            # Size position
                            if lot_size_mode == "fixed":
                                lots = fixed_lots
                            else:
                                lots = math.floor(capital / 100000.0)
                                if lots == 0:
                                    lots = 1
                            total_shares = lots * lot_size
                            
                            # Costs: flat ₹40 round-trip brokerage + 0.05 pts slippage per share
                            brokerage = 40.0
                            slippage = 0.05 * total_shares
                            
                            if trade_result in ("WIN", "LOSS", "TIMEOUT"):
                                pnl_gross = (entry - exit_price_val) * total_shares
                                pnl_net = pnl_gross - brokerage - slippage
                            else:
                                pnl_net = 0.0
                                
                            session_trades.append({
                                "date": str(d),
                                "side": "SELL FUTURES",
                                "symbol": "NIFTY FUTURES",
                                "entry_time": time_str,
                                "exit_time": exit_time,
                                "duration": f"{duration}m" if trade_result != "OPEN" else "-",
                                "entry_spot": entry,
                                "exit_spot": exit_price_val,
                                "entry_premium": entry,
                                "exit_premium": exit_price_val,
                                "result": trade_result,
                                "pnl": pnl_net,
                                "lots": lots
                            })
                        
                        s_trough = None
                        s_peak = None
                        s_stage = 1
                        
        # Compute daily stats
        wins = sum(1 for t in session_trades if t["result"] == "WIN")
        losses = sum(1 for t in session_trades if t["result"] == "LOSS")
        be = sum(1 for t in session_trades if t["result"] == "BREAKEVEN")
        opens = sum(1 for t in session_trades if t["result"] == "OPEN")
        total_pnl = sum(t["pnl"] for t in session_trades)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        
        daily_summaries.append({
            "date": str(d),
            "trades": len(session_trades),
            "wins": wins,
            "losses": losses,
            "be": be,
            "open": opens,
            "win_rate": round(win_rate, 1),
            "pnl": round(total_pnl)
        })
        trades.extend(session_trades)
        
    # Compute overall summary
    total_wins = sum(s["wins"] for s in daily_summaries)
    total_losses = sum(s["losses"] for s in daily_summaries)
    total_be = sum(s["be"] for s in daily_summaries)
    total_pnl_all = sum(s["pnl"] for s in daily_summaries)
    overall_win_rate = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0.0
    
    summary = {
        "total_trades": sum(s["trades"] for s in daily_summaries),
        "wins": total_wins,
        "losses": total_losses,
        "be": total_be,
        "win_rate": round(overall_win_rate, 1),
        "total_pnl": round(total_pnl_all)
    }
    
    return {
        "summary": summary,
        "daily": daily_summaries,
        "trades": trades
    }


_LIVE_POSITION = {
    "active": False,
    "symbol": None,
    "side": None,
    "lots": 1,
    "entry_spot": 0.0,
    "entry_opt_price": 0.0,
    "opt_symbol": None,
    "opt_token": None,
    "lot_size": 1,
    "sl_spot": 0.0,
    "target_spot": 0.0,
    "is_sar": False,
    "sar_done": False,
    "atr": 0.0,
    "order_id": None,
    "exit_order_id": None,
    "status": "OPEN",
    "result": None,
    "exit_price": 0.0
}

def _get_atr_for_symbol(kc, symbol):
    try:
        formatted_sym = symbol
        if symbol in ("NIFTY", "NIFTY 50"):
            formatted_sym = "NSE:NIFTY 50"
        elif symbol in ("BANKNIFTY", "NIFTY BANK"):
            formatted_sym = "NSE:NIFTY BANK"
        else:
            if not symbol.startswith("NSE:"):
                formatted_sym = "NSE:" + symbol
                
        # Resolve instrument token
        imap = Z.instrument_map(kc)
        tok = imap.get(symbol) or imap.get(formatted_sym) or imap.get(formatted_sym.replace("NSE:", ""))
        if not tok:
            insts = kc.instruments("NSE")
            m = next((i for i in insts if i.get("tradingsymbol") == symbol or i.get("name") == symbol), None)
            if m:
                tok = int(m["instrument_token"])
        
        if not tok:
            return 10.0
            
        to_date = datetime.datetime.now()
        from_date = to_date - datetime.timedelta(hours=4)
        candles = kc.historical_data(tok, from_date, to_date, "minute")
        
        if len(candles) < 15:
            return 10.0
            
        tr_history = []
        for idx, c in enumerate(candles):
            high = float(c["high"])
            low = float(c["low"])
            close = float(c["close"])
            if idx == 0:
                tr = high - low
            else:
                prev_close = float(candles[idx-1]["close"])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_history.append(tr)
            
        atr = tr_history[0]
        for tr in tr_history[1:]:
            atr = (atr * 13.0 + tr) / 14.0
        return round(atr, 2)
    except Exception as e:
        print(f"[live] ATR lookup failed for {symbol}, defaulting to 10.0: {e}", flush=True)
        return 10.0

def _exit_live_position(kc, reason):
    try:
        opt_symbol = _LIVE_POSITION["opt_symbol"]
        lots = _LIVE_POSITION["lots"]
        lot_size = _LIVE_POSITION["lot_size"]
        
        opt_quote = kc.quote([f"NFO:{opt_symbol}"])
        q_data = opt_quote.get(f"NFO:{opt_symbol}", {})
        best_bid = q_data.get("depth", {}).get("buy", [{}])[0].get("price") or q_data.get("last_price")
        
        if not best_bid:
            print(f"[live-monitor] Failed to get sell quote for {opt_symbol}, cannot place exit order.", flush=True)
            return
            
        exit_price = _round_tick(best_bid, _get_tick_size(kc, "NFO", opt_symbol))
        
        oid = kc.place_order(
            variety=kc.VARIETY_REGULAR,
            exchange=kc.EXCHANGE_NFO,
            tradingsymbol=opt_symbol,
            transaction_type=kc.TRANSACTION_TYPE_SELL,
            quantity=int(lots * lot_size),
            product=kc.PRODUCT_NRML,
            order_type=kc.ORDER_TYPE_LIMIT,
            price=exit_price
        )
        
        _LIVE_POSITION["status"] = "CLOSED"
        _LIVE_POSITION["result"] = "TARGET" if "Target" in reason else "SL" if "Stop Loss" in reason else "MANUAL"
        _LIVE_POSITION["exit_price"] = exit_price
        _LIVE_POSITION["exit_order_id"] = oid
        _LIVE_POSITION["active"] = False
        print(f"[live-monitor] Exit order placed: {oid} at price {exit_price}. Reason: {reason}", flush=True)
    except Exception as e:
        print(f"[live-monitor] Failed to exit position: {e}", flush=True)
        _LIVE_POSITION["active"] = False

def _execute_live_sar(kc, symbol, rev_side, entry_spot_price):
    try:
        opt = _resolve_option(kc, symbol, entry_spot_price, rev_side)
        if not opt:
            print(f"[live-monitor] Failed to resolve reverse option for SAR: {symbol} {rev_side}", flush=True)
            return
            
        tradingsymbol = opt["tradingsymbol"]
        lot_size = opt["lot_size"]
        lots = _LIVE_POSITION.get("lots", 1)
        
        opt_quote = kc.quote([f"NFO:{tradingsymbol}"])
        q_data = opt_quote.get(f"NFO:{tradingsymbol}", {})
        best_ask = q_data.get("depth", {}).get("sell", [{}])[0].get("price") or q_data.get("last_price")
        
        if not best_ask:
            print(f"[live-monitor] Failed to get buy quote for reverse option {tradingsymbol}", flush=True)
            return
            
        price = _round_tick(best_ask, _get_tick_size(kc, "NFO", tradingsymbol))
        
        oid = kc.place_order(
            variety          = kc.VARIETY_REGULAR,
            exchange         = kc.EXCHANGE_NFO,
            tradingsymbol    = tradingsymbol,
            transaction_type = kc.TRANSACTION_TYPE_BUY,
            quantity         = int(lots * lot_size),
            product          = kc.PRODUCT_NRML,
            order_type       = kc.ORDER_TYPE_LIMIT,
            price            = price
        )
        
        if rev_side == "CE":
            target_spot = entry_spot_price + 50.0
            sl_spot = entry_spot_price - 50.0
        else:
            target_spot = entry_spot_price - 50.0
            sl_spot = entry_spot_price + 50.0
            
        _LIVE_POSITION.update({
            "active": True,
            "symbol": symbol,
            "side": rev_side,
            "lots": lots,
            "entry_spot": entry_spot_price,
            "entry_opt_price": price,
            "opt_symbol": tradingsymbol,
            "opt_token": opt.get("instrument_token"),
            "lot_size": lot_size,
            "sl_spot": sl_spot,
            "target_spot": target_spot,
            "is_sar": True,
            "sar_done": True,
            "order_id": oid,
            "exit_order_id": None,
            "status": "OPEN",
            "result": None,
            "exit_price": 0.0
        })
        print(f"[live-monitor] SAR position entered: {rev_side} {tradingsymbol} at price {price}. Target Spot: {target_spot}, SL Spot: {sl_spot}", flush=True)
    except Exception as e:
        print(f"[live-monitor] Error placing SAR order: {e}", flush=True)

def _live_position_monitor_loop():
    import sys
    while True:
        try:
            if not sys or not sys.modules or sys.is_finalizing():
                break
            if _LIVE_POSITION.get("active") and _LIVE_POSITION.get("status") == "OPEN":
                symbol = _LIVE_POSITION["symbol"]
                side = _LIVE_POSITION["side"]
                sl_spot = _LIVE_POSITION["sl_spot"]
                target_spot = _LIVE_POSITION.get("target_spot", 0.0)
                is_sar = _LIVE_POSITION.get("is_sar", False)
                sar_done = _LIVE_POSITION.get("sar_done", False)
                
                formatted_sym = symbol
                if symbol in ("NIFTY", "NIFTY 50"):
                    formatted_sym = "NSE:NIFTY 50"
                elif symbol in ("BANKNIFTY", "NIFTY BANK"):
                    formatted_sym = "NSE:NIFTY BANK"
                else:
                    if not symbol.startswith("NSE:"):
                        formatted_sym = "NSE:" + symbol
                        
                if Z.auth_status():
                    kc = Z.kite()
                    quote = kc.quote([formatted_sym])
                    spot_price = quote.get(formatted_sym, {}).get("last_price")
                    
                    if spot_price:
                        # 1. Check Target Hit
                        hit_target = False
                        if target_spot > 0.0:
                            if side == "CE" and spot_price >= target_spot:
                                hit_target = True
                            elif side == "PE" and spot_price <= target_spot:
                                hit_target = True
                                
                        if hit_target:
                            reason = f"Target Hit (Spot reached {target_spot:.2f})"
                            print(f"[live-monitor] Triggering exit for {symbol} {side}: {reason} (Spot: {spot_price})", flush=True)
                            _exit_live_position(kc, reason)
                            continue
                            
                        # 2. Check Stop Loss Hit
                        hit_sl = False
                        if side == "CE" and spot_price <= sl_spot:
                            hit_sl = True
                        elif side == "PE" and spot_price >= sl_spot:
                            hit_sl = True
                            
                        if hit_sl:
                            reason = f"Stop Loss Hit (Spot reached {sl_spot:.2f})"
                            print(f"[live-monitor] Triggering exit for {symbol} {side}: {reason} (Spot: {spot_price})", flush=True)
                            _exit_live_position(kc, reason)
                            
                            # Check if we should trigger Stop-and-Reverse (SAR)
                            if not is_sar and not sar_done:
                                rev_side = "PE" if side == "CE" else "CE"
                                print(f"[live-monitor] TRIGGERING STOP-AND-REVERSE (SAR): Buying {rev_side}...", flush=True)
                                _execute_live_sar(kc, symbol, rev_side, sl_spot)
                            continue
        except Exception as e:
            print(f"[live-monitor] Error: {e}", flush=True)
        time.sleep(1.0)

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        path = self.path.split("?", 1)[0]          # ignore query string when routing
        # Kite redirects back here as a GET with ?request_token=...
        if path == "/api/auth/token":
            qs = parse_qs(urlparse(self.path).query)
            rt = qs.get("request_token", [""])[0]
            if not rt:
                page = b"<html><body><h2>Missing request_token</h2></body></html>"
                return self._send(400, page, "text/html")
            try:
                user = Z.exchange_token(rt)
                page = (
                    b"<html><head><meta http-equiv='refresh' content='1;url=/'></head>"
                    b"<body style='font-family:sans-serif;padding:40px'>"
                    b"<h2 style='color:#16a34a'>&#10003; Zerodha connected!</h2>"
                    b"<p>Redirecting to app&hellip;</p></body></html>"
                )
                return self._send(200, page, "text/html")
            except Exception as e:
                page = f"<html><body><h2>Auth failed: {e}</h2></body></html>".encode()
                return self._send(200, page, "text/html")
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                html = f.read().decode("utf-8")
            mock_ws = os.getenv("MOCK_WS_URL", "ws://localhost:8765")
            inject = f'<script>window._MOCK_WS_URL={json.dumps(mock_ws)};</script>'
            html = html.replace("</head>", inject + "\n</head>", 1)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if path in ("/premarket", "/premarket.html"):
            with open(os.path.join(HERE, "web", "premarket.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/options/gap_recommendation":
            try:
                if not Z.auth_status():
                    return self._send(200, dumps({"error": "connect Zerodha first"}))
                kc = Z.kite()
                nifty_token = 256265
                today = datetime.date.today()
                
                # Resolve yesterday close
                yesterday = today - datetime.timedelta(days=1)
                while yesterday.weekday() >= 5:
                    yesterday -= datetime.timedelta(days=1)
                    
                y_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
                if not y_candles:
                    y_start = datetime.datetime.combine(yesterday, datetime.time(9,15))
                    y_end = datetime.datetime.combine(yesterday, datetime.time(15,30))
                    y_candles_m = kc.historical_data(nifty_token, y_start, y_end, "minute")
                    yesterday_close = y_candles_m[-1]['close'] if y_candles_m else None
                else:
                    yesterday_close = y_candles[0]['close']
                    
                if not yesterday_close:
                    return self._send(200, dumps({"status": "error", "message": "Could not resolve yesterday close price."}))
                    
                # Resolve today open
                today_open = None
                t_candles = kc.historical_data(nifty_token, today, today, "day")
                if t_candles:
                    today_open = t_candles[0]['open']
                else:
                    t_start = datetime.datetime.combine(today, datetime.time(9,15))
                    t_end = datetime.datetime.now()
                    t_candles_m = kc.historical_data(nifty_token, t_start, t_end, "minute")
                    if t_candles_m:
                        today_open = t_candles_m[0]['open']
                        
                quote = kc.quote(["NSE:NIFTY 50"])
                spot_ltp = quote.get("NSE:NIFTY 50", {}).get("last_price")
                
                if not today_open:
                    return self._send(200, dumps({
                        "status": "waiting_for_open",
                        "yesterday_close": yesterday_close,
                        "spot_ltp": spot_ltp
                    }))
                    
                gap = today_open - yesterday_close
                gap_pct = gap / yesterday_close
                
                if gap_pct >= 0.008:
                    side = "PE"
                    mode = "FADE"
                    desc = f"PE (Fade Extreme Gap Up {gap_pct*100:+.2f}%)"
                elif gap_pct <= -0.008:
                    side = "CE"
                    mode = "FADE"
                    desc = f"CE (Fade Extreme Gap Down {gap_pct*100:+.2f}%)"
                elif gap > 0:
                    side = "CE"
                    mode = "FOLLOW"
                    desc = f"CE (Follow Normal Gap Up {gap_pct*100:+.2f}%)"
                else:
                    side = "PE"
                    mode = "FOLLOW"
                    desc = f"PE (Follow Normal Gap Down {gap_pct*100:+.2f}%)"
                    
                return self._send(200, dumps({
                    "status": "active",
                    "yesterday_close": yesterday_close,
                    "today_open": today_open,
                    "gap_points": round(gap, 2),
                    "gap_pct": round(gap_pct * 100, 2),
                    "side": side,
                    "mode": mode,
                    "desc": desc,
                    "spot_ltp": spot_ltp
                }))
            except Exception as e:
                return self._send(200, dumps({"status": "error", "message": str(e)}))
        if path == "/api/reversal":
            import reversal_scanner as RS
            try:
                result = RS.run(Z.kite())
                return self._send(200, dumps({"candidates": result}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e), "candidates": []}))
        if path == "/api/rec/backtest":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            market = q.get("market", ["IN"])[0].upper()
            if market not in ("IN", "US"):
                market = "IN"
            days = int(q.get("days", ["15"])[0])
            try:
                results = REC.backtest(market=market, days=days, kc=Z.kite())
                return self._send(200, dumps({"rows": results}))
            except Exception as e:
                return self._send(200, dumps({"error": f"{type(e).__name__}: {e}", "rows": []}))
        if path == "/api/trading/tokens":
            from scanner import TRADING_LIST
            try:
                imap = Z.instrument_map(Z.kite())
                result = {s: imap[s] for s in TRADING_LIST if s in imap}
                return self._send(200, dumps(result))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/status":
            from urllib.parse import urlparse, parse_qs
            sym = parse_qs(urlparse(self.path).query).get("symbol", [""])[0].upper()
            b = _BRACKET.get(sym)
            if not b:
                return self._send(200, dumps({"symbol": sym, "open": False}))
            return self._send(200, dumps({
                "symbol": sym, "open": not b["closed"], "result": b["result"],
                "exit_price": b["exit_price"], "entry_price": b["entry_price"],
                "tradingsymbol": b["tradingsymbol"], "quantity": b["quantity"],
            }))
        if path == "/api/trading/mock-tokens":
            return self._send(200, dumps({"TESTBUY": 341249, "TESTSELL": 408065}))
        if path == "/api/trading/resolve":
            from urllib.parse import urlparse, parse_qs
            syms_str = parse_qs(urlparse(self.path).query).get("symbols", [""])[0]
            syms = [s.strip().upper() for s in syms_str.split(",") if s.strip()]
            if not syms:
                return self._send(200, dumps({"tokens": {}, "missing": []}))
            try:
                imap = Z.instrument_map(Z.kite())
                result  = {s: imap[s] for s in syms if s in imap}
                missing = [s for s in syms if s not in imap]
                return self._send(200, dumps({"tokens": result, "missing": missing}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/defaults":
            return self._send(200, dumps(defaults()))
        if path == "/api/auth/status":
            return self._send(200, dumps({"connected": Z.auth_status(), "source": config.SOURCE}))
        if path == "/api/live/state":
            return self._send(200, dumps(LIVE.state()))
        if path == "/api/live/position":
            return self._send(200, dumps(_LIVE_POSITION))
        if path == "/api/live/logs":
            try:
                log_dir = os.path.join(HERE, "data", "live_signals")
                if not os.path.exists(log_dir):
                    return self._send(200, dumps({"files": []}))
                files = sorted([f for f in os.listdir(log_dir) if f.startswith("signals_") and f.endswith(".json")], reverse=True)
                return self._send(200, dumps({"files": files}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e), "files": []}))
                
        if path == "/api/live/logs/download":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            file_name = q.get("file", [""])[0].strip()
            if not file_name or ".." in file_name or "/" in file_name or "\\" in file_name:
                return self._send(400, b"Invalid filename", "text/plain")
            try:
                file_path = os.path.join(HERE, "data", "live_signals", file_name)
                if not os.path.exists(file_path):
                    return self._send(404, b"File not found", "text/plain")
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Disposition", f"attachment; filename={file_name}")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            except Exception as e:
                return self._send(500, str(e).encode(), "text/plain")
        if path == "/api/recommend":
            import datetime as _dt
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            market = q.get("market", ["IN"])[0].upper()
            if market not in ("IN", "US"):
                market = "IN"
            today = str(_dt.date.today())
            c = _REC_CACHE.get(market)
            if not c or c["date"] != today or "refresh" in q:
                try:
                    _REC_CACHE[market] = {"date": today, "data": REC.daily_pick(market=market)}
                except Exception as e:
                    return self._send(200, dumps({"error": f"{type(e).__name__}: {e}"}))
            return self._send(200, dumps(_REC_CACHE[market]["data"]))
        if path == "/api/newsflash":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            market = q.get("market", ["IN"])[0].upper()
            if market not in ("IN", "US"):
                market = "IN"
            return self._send(200, dumps(NF.get_radar(market).feed()))
        if path == "/api/auth/url":
            try:
                return self._send(200, dumps({"url": Z.login_url()}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/auth/credentials":
            tok = Z.load_token()
            if not tok:
                return self._send(200, dumps({"error": "No valid token — connect first"}))
            api_key, _ = Z._creds()
            return self._send(200, dumps({"api_key": api_key, "access_token": tok}))
        if path.startswith("/api/instruments/"):
            exchange = path.split("/")[-1].upper()
            try:
                kc = Z.kite()
                import io, csv as _csv
                rows = kc.instruments(exchange)
                out = io.StringIO()
                if rows:
                    w = _csv.DictWriter(out, fieldnames=rows[0].keys())
                    w.writeheader(); w.writerows(rows)
                return self._send(200, out.getvalue().encode(), "text/csv")
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/article":
            from urllib.parse import urlparse, parse_qs
            url = parse_qs(urlparse(self.path).query).get("url", [""])[0].strip()
            if not url:
                return self._send(200, dumps({"error": "?url= required"}))
            try:
                import requests as req, re as _re
                r = req.get(url, timeout=12, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}, allow_redirects=True)
                raw = r.text
                # strip scripts/styles then tags
                raw = _re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", raw)
                raw = _re.sub(r"<[^>]+>", " ", raw)
                raw = _re.sub(r"&amp;","&",raw); raw = _re.sub(r"&lt;","<",raw)
                raw = _re.sub(r"&gt;",">",raw);  raw = _re.sub(r"&nbsp;"," ",raw)
                text = _re.sub(r"\s+", " ", raw).strip()[:2000]
                return self._send(200, dumps({"text": text}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/globalnews":
            import globalnews as GN
            try:
                return self._send(200, dumps(GN.fetch_global_news()))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/predictnext":
            from urllib.parse import urlparse, parse_qs
            sym = parse_qs(urlparse(self.path).query).get("symbol", [""])[0].strip()
            if not sym:
                return self._send(200, dumps({"error": "?symbol= required"}))
            import globalnews as GN
            try:
                return self._send(200, dumps(GN.predict_next_day(sym)))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/stocknews":
            from urllib.parse import urlparse, parse_qs
            sym = parse_qs(urlparse(self.path).query).get("symbol", [""])[0].strip()
            if not sym:
                return self._send(200, dumps({"error": "?symbol= required"}))
            import globalnews as GN
            try:
                return self._send(200, dumps(GN.fetch_stock_news(sym)))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/indianews":
            import globalnews as GN
            try:
                return self._send(200, dumps(GN.fetch_india_news()))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path.startswith("/api/scanner/download"):
            import data_store as DS
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            date_str   = qs.get("date",    [""])[0]
            snap_type  = qs.get("type",    ["premarket"])[0]
            session_id = qs.get("session", [""])[0]
            if snap_type == "session" and date_str and session_id:
                raw   = DS.get_session_raw(date_str, session_id)
                fname = f"session_{date_str}_{session_id}.json"
            else:
                raw   = DS.get_raw(date_str) if date_str else {}
                fname = f"premarket_{date_str or 'today'}.json"
            b = json.dumps(raw, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == "/api/scanner/history":
            import data_store as DS
            return self._send(200, dumps(DS.get_history()))
        if path == "/api/scanner/sessions":
            import data_store as DS
            from urllib.parse import urlparse, parse_qs
            date_str = parse_qs(urlparse(self.path).query).get("date", [""])[0]
            if not date_str:
                import datetime as _dt
                ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
                date_str = str(ist.date())
            return self._send(200, dumps(DS.get_sessions(date_str)))
        if path == "/api/scanner/enrich":
            import data_store as DS
            from urllib.parse import urlparse, parse_qs
            date_str = parse_qs(urlparse(self.path).query).get("date", [""])[0]
            if not date_str:
                import datetime as _dt
                ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
                date_str = str(ist.date())
            try:
                kc = Z.kite()
                return self._send(200, dumps(DS.enrich(date_str, kc)))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/autotest/today":
            import auto_test as AXT, datetime as _dt
            ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
            return self._send(200, dumps(AXT.get_today(str(ist.date()))))
        if path == "/api/autotest/history":
            import auto_test as AXT
            return self._send(200, dumps(AXT.get_history()))
        if path == "/api/autotrader/status":
            import autotrader as AT
            return self._send(200, dumps({"buy": AT.BUYER.status(), "sell": AT.SELLER.status()}))
        if path == "/api/options/autotrader/status":
            return self._send(200, dumps(options_trader.status()))
        if path == "/api/oi_metrics":
            return self._send(200, dumps(options_trader.get_live_oi_metrics()))
        if path == "/api/options/execute_spread":
            from urllib.parse import urlparse, parse_qs
            try:
                params = parse_qs(urlparse(self.path).query)
                spread_type = params.get("type", [""])[0]
                sell_strike = int(params.get("sell_strike", [0])[0])
                buy_strike = int(params.get("buy_strike", [0])[0])
                qty = int(params.get("qty", [65])[0])
                
                if not spread_type or not sell_strike or not buy_strike:
                    return self._send(200, dumps({"error": "Missing parameters (type, sell_strike, buy_strike)"}))
                    
                kc = Z.kite()
                today_date = options_trader._ist().date()
                expiry_date = Z.get_expiry_date(kc, today_date)
                
                opt_kind = "PE" if "PUT" in spread_type else "CE"
                
                sell_tok = Z.get_option_token(kc, "NIFTY", expiry_date, sell_strike, opt_kind)
                buy_tok = Z.get_option_token(kc, "NIFTY", expiry_date, buy_strike, opt_kind)
                
                insts = Z.get_nfo_instruments(kc)
                
                sell_sym = next((i["tradingsymbol"] for i in insts if int(i.get("instrument_token") or 0) == sell_tok), None)
                buy_sym = next((i["tradingsymbol"] for i in insts if int(i.get("instrument_token") or 0) == buy_tok), None)
                
                if not sell_sym or not buy_sym:
                    return self._send(200, dumps({"error": f"Option symbols not found for strikes {sell_strike} / {buy_strike}"}))
                
                # SEQUENCED EXECUTION: Buy hedge first, then Sell to secure margin benefit
                # 1. Place BUY order
                buy_order_id = kc.place_order(
                    variety=kc.VARIETY_REGULAR,
                    exchange=kc.EXCHANGE_NFO,
                    tradingsymbol=buy_sym,
                    transaction_type=kc.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    product=kc.PRODUCT_NRML,
                    order_type=kc.ORDER_TYPE_MARKET
                )
                
                # 2. Place SELL order
                sell_order_id = kc.place_order(
                    variety=kc.VARIETY_REGULAR,
                    exchange=kc.EXCHANGE_NFO,
                    tradingsymbol=sell_sym,
                    transaction_type=kc.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    product=kc.PRODUCT_NRML,
                    order_type=kc.ORDER_TYPE_MARKET
                )
                
                return self._send(200, dumps({
                    "success": True,
                    "buy_order_id": buy_order_id,
                    "sell_order_id": sell_order_id,
                    "message": f"Successfully executed spread orders for NIFTY {sell_strike} / {buy_strike} {opt_kind} (Qty: {qty})"
                }))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/sector_values":
            try:
                kc = Z.kite()
                sec_syms = [
                    "NSE:NIFTY BANK",
                    "NSE:NIFTY IT",
                    "NSE:NIFTY AUTO",
                    "NSE:NIFTY FMCG",
                    "NSE:NIFTY METAL",
                    "NSE:NIFTY PHARMA"
                ]
                quotes = kc.quote(sec_syms)
                out = {}
                for k, q in quotes.items():
                    name = k.replace("NSE:", "")
                    ltp = q.get("last_price", 0)
                    close = q.get("ohlc", {}).get("close", 0)
                    chg = round(((ltp - close) / close * 100), 2) if close > 0 else 0.0
                    out[name] = {"ltp": ltp, "change": chg}
                return self._send(200, dumps(out))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/autotrader/stop":
            import autotrader as AT
            from urllib.parse import urlparse, parse_qs
            side = parse_qs(urlparse(self.path).query).get("side", ["both"])[0]
            if side in ("buy",  "both"): AT.BUYER.stop()
            if side in ("sell", "both"): AT.SELLER.stop()
            return self._send(200, dumps({"buy": AT.BUYER.status(), "sell": AT.SELLER.status()}))
        if path == "/api/scanner/tokens":
            import scanner as SC
            from urllib.parse import urlparse, parse_qs
            mode = parse_qs(urlparse(self.path).query).get("mode", ["daily"])[0]
            wl = set(SC.WATCHLIST if mode == "premarket" else SC.WATCHLIST_DAILY)
            try:
                kc = Z.kite()
                rows = kc.instruments("NSE")
                result = {r["tradingsymbol"]: r["instrument_token"]
                          for r in rows if r["tradingsymbol"] in wl
                          and r.get("instrument_type") == "EQ"}
                return self._send(200, dumps(result))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/option/token":
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            symbol   = params.get("symbol",  [""])[0].upper()
            try:
                strike = float(params.get("strike", [0])[0])
            except Exception:
                strike = 0.0
            opt_type = params.get("type", ["CE"])[0].upper()
            try:
                kc = Z.kite()
                instruments = kc.instruments("NFO")
                today = datetime.date.today()
                matches = [i for i in instruments
                           if i.get("name") == symbol
                           and float(i.get("strike") or 0) == strike
                           and i.get("instrument_type") == opt_type
                           and i.get("expiry") and i["expiry"] >= today]
                if not matches:
                    return self._send(404, dumps({"error": "not found"}))
                matches.sort(key=lambda x: x["expiry"])
                m = matches[0]
                return self._send(200, dumps({
                    "token":         m["instrument_token"],
                    "tradingsymbol": m["tradingsymbol"],
                    "expiry":        str(m["expiry"]),
                    "lot_size":      int(m.get("lot_size") or 500),
                }))
            except Exception as e:
                return self._send(500, dumps({"error": str(e)}))
        if path == "/api/premarket/dummy":
            return self._dummy_stream()
        self._send(404, dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        if path == "/api/auth/token":
            try:
                user = Z.exchange_token(body.get("request_token", ""))
                return self._send(200, dumps({"connected": True, "user": user}))
            except Exception as e:
                return self._send(200, dumps({"error": f"{type(e).__name__}: {e}"}))
        if path == "/api/live/start":
            if not Z.auth_status():
                return self._send(200, dumps({"error": "connect Zerodha first"}))
            LIVE.start()
            return self._send(200, dumps(LIVE.state()))
        if path == "/api/live/stop":
            LIVE.stop()
            return self._send(200, dumps(LIVE.state()))
        if path == "/api/live/log_signal":
            sym = body.get("symbol", "").strip().upper()
            sig = body.get("signal", "").strip() # "Buyers" or "Sellers"
            if not sym or not sig:
                return self._send(200, dumps({"error": "symbol and signal are required"}))
            try:
                import pytz
                log_dir = os.path.join(HERE, "data", "live_signals")
                os.makedirs(log_dir, exist_ok=True)
                
                ist_tz = pytz.timezone("Asia/Kolkata")
                now = datetime.datetime.now(ist_tz)
                date_str = now.strftime("%Y%m%d")
                file_name = f"signals_{date_str}.json"
                file_path = os.path.join(log_dir, file_name)
                
                logs = []
                if os.path.exists(file_path):
                    try:
                        with open(file_path, "r") as f:
                            logs = json.load(f)
                    except Exception as je:
                        print(f"[live-log] Failed to parse JSON, starting fresh: {je}", flush=True)
                        logs = []
                        
                time_str = now.strftime("%H:%M:%S.%f")[:-3]
                logs.append({
                    "timestamp": time_str,
                    "symbol": sym,
                    "signal": sig
                })
                
                with open(file_path, "w") as f:
                    json.dump(logs, f, indent=2)
                    
                return self._send(200, dumps({"status": "success", "file": file_name}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/options/execute_gap_play":
            if _LIVE_POSITION.get("active"):
                return self._send(200, dumps({"error": "An active live position is already running."}))
            try:
                if not Z.auth_status():
                    return self._send(200, dumps({"error": "connect Zerodha first"}))
                kc = Z.kite()
                lots = int(body.get("lots", 2))
                
                nifty_token = 256265
                today = datetime.date.today()
                
                # Resolve yesterday close
                yesterday = today - datetime.timedelta(days=1)
                while yesterday.weekday() >= 5:
                    yesterday -= datetime.timedelta(days=1)
                    
                y_candles = kc.historical_data(nifty_token, yesterday, yesterday, "day")
                if not y_candles:
                    y_start = datetime.datetime.combine(yesterday, datetime.time(9,15))
                    y_end = datetime.datetime.combine(yesterday, datetime.time(15,30))
                    y_candles_m = kc.historical_data(nifty_token, y_start, y_end, "minute")
                    yesterday_close = y_candles_m[-1]['close'] if y_candles_m else None
                else:
                    yesterday_close = y_candles[0]['close']
                    
                if not yesterday_close:
                    return self._send(200, dumps({"error": "Could not resolve yesterday close price."}))
                    
                # Resolve today open
                today_open = None
                t_candles = kc.historical_data(nifty_token, today, today, "day")
                if t_candles:
                    today_open = t_candles[0]['open']
                else:
                    t_start = datetime.datetime.combine(today, datetime.time(9,15))
                    t_end = datetime.datetime.now()
                    t_candles_m = kc.historical_data(nifty_token, t_start, t_end, "minute")
                    if t_candles_m:
                        today_open = t_candles_m[0]['open']
                        
                if not today_open:
                    return self._send(200, dumps({"error": "Waiting for market open price."}))
                    
                quote = kc.quote(["NSE:NIFTY 50"])
                spot_ltp = quote.get("NSE:NIFTY 50", {}).get("last_price")
                if not spot_ltp:
                    return self._send(200, dumps({"error": "Could not resolve Nifty Spot LTP."}))
                    
                gap = today_open - yesterday_close
                gap_pct = gap / yesterday_close
                
                if gap_pct >= 0.008:
                    side = "PE"
                elif gap_pct <= -0.008:
                    side = "CE"
                elif gap > 0:
                    side = "CE"
                else:
                    side = "PE"
                    
                # Resolve option
                opt = _resolve_option(kc, "NIFTY", spot_ltp, side)
                if not opt:
                    return self._send(200, dumps({"error": f"No nearest {side} option found for NIFTY at strike near {spot_ltp}"}))
                    
                tradingsymbol = opt["tradingsymbol"]
                lot_size = opt["lot_size"]
                
                opt_quote = kc.quote([f"NFO:{tradingsymbol}"])
                q_data = opt_quote.get(f"NFO:{tradingsymbol}", {})
                best_ask = q_data.get("depth", {}).get("sell", [{}])[0].get("price") or q_data.get("last_price")
                
                if not best_ask:
                    return self._send(200, dumps({"error": f"Could not get option quote for {tradingsymbol}"}))
                    
                price = _round_tick(best_ask, _get_tick_size(kc, "NFO", tradingsymbol))
                
                if side == "CE":
                    target_spot = spot_ltp + 40.0
                    sl_spot = spot_ltp - 50.0
                else:
                    target_spot = spot_ltp - 40.0
                    sl_spot = spot_ltp + 50.0
                    
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_BUY,
                    quantity         = int(lots * lot_size),
                    product          = kc.PRODUCT_NRML,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = price
                )
                
                _LIVE_POSITION.update({
                    "active": True,
                    "symbol": "NIFTY",
                    "side": side,
                    "lots": lots,
                    "entry_spot": spot_ltp,
                    "entry_opt_price": price,
                    "opt_symbol": tradingsymbol,
                    "opt_token": opt.get("instrument_token"),
                    "lot_size": lot_size,
                    "sl_spot": sl_spot,
                    "target_spot": target_spot,
                    "is_sar": False,
                    "sar_done": False,
                    "order_id": oid,
                    "exit_order_id": None,
                    "status": "OPEN",
                    "result": None,
                    "exit_price": 0.0
                })
                
                print(f"[live-trading] Gap Play executed: {side} option {tradingsymbol} ({lots} lots) at price {price}. Spot: {spot_ltp}, Target Spot: {target_spot}, SL Spot: {sl_spot}", flush=True)
                return self._send(200, dumps(_LIVE_POSITION))
            except Exception as e:
                traceback.print_exc()
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/live/buy":
            if _LIVE_POSITION.get("active"):
                return self._send(200, dumps({"error": "An active live position is already running."}))
            
            raw_sym = body.get("symbol", "").strip().upper()
            side = body.get("side", "CE").strip().upper()
            lots = int(body.get("lots", 1))
            
            if not raw_sym or side not in ("CE", "PE"):
                return self._send(200, dumps({"error": "symbol and side (CE/PE) are required."}))
                
            sym = raw_sym
            if sym in ("NIFTY 50", "NSE:NIFTY 50"):
                sym = "NIFTY"
            elif sym in ("NIFTY BANK", "BANKNIFTY", "NSE:NIFTY BANK"):
                sym = "BANKNIFTY"
                
            try:
                kc = Z.kite()
                formatted_sym = raw_sym
                if raw_sym in ("NIFTY", "NIFTY 50", "NIFTY 50 SPOT"):
                    formatted_sym = "NSE:NIFTY 50"
                    sym = "NIFTY"
                elif raw_sym in ("BANKNIFTY", "NIFTY BANK", "NIFTY BANK SPOT"):
                    formatted_sym = "NSE:NIFTY BANK"
                    sym = "BANKNIFTY"
                else:
                    if not raw_sym.startswith("NSE:"):
                        formatted_sym = "NSE:" + raw_sym
                        
                quote = kc.quote([formatted_sym])
                spot_ltp = quote.get(formatted_sym, {}).get("last_price")
                if not spot_ltp:
                    return self._send(200, dumps({"error": f"Could not get quote for {formatted_sym}"}))
                    
                atr = _get_atr_for_symbol(kc, raw_sym)
                sl_points = 2.0 * atr
                
                opt = _resolve_option(kc, sym, spot_ltp, side)
                if not opt:
                    return self._send(200, dumps({"error": f"No nearest {side} option found for {sym} at strike near {spot_ltp}"}))
                    
                tradingsymbol = opt["tradingsymbol"]
                lot_size = opt["lot_size"]
                
                opt_quote = kc.quote([f"NFO:{tradingsymbol}"])
                q_data = opt_quote.get(f"NFO:{tradingsymbol}", {})
                best_ask = q_data.get("depth", {}).get("sell", [{}])[0].get("price") or q_data.get("last_price")
                
                if not best_ask:
                    return self._send(200, dumps({"error": f"Could not get option quote for {tradingsymbol}"}))
                    
                price = _round_tick(best_ask, _get_tick_size(kc, "NFO", tradingsymbol))
                
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_BUY,
                    quantity         = int(lots * lot_size),
                    product          = kc.PRODUCT_NRML,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = price
                )
                
                _LIVE_POSITION.update({
                    "active": True,
                    "symbol": raw_sym,
                    "side": side,
                    "lots": lots,
                    "entry_spot": spot_ltp,
                    "entry_opt_price": price,
                    "opt_symbol": tradingsymbol,
                    "opt_token": opt.get("instrument_token"),
                    "lot_size": lot_size,
                    "sl_spot": (spot_ltp - sl_points) if side == "CE" else (spot_ltp + sl_points),
                    "atr": atr,
                    "order_id": oid,
                    "exit_order_id": None,
                    "status": "OPEN",
                    "result": None,
                    "exit_price": 0.0
                })
                
                print(f"[live-trading] Entered {side} position on {raw_sym}: {lots} lots of {tradingsymbol} at price {price}. Spot entry: {spot_ltp}, SL Spot: {_LIVE_POSITION['sl_spot']}", flush=True)
                return self._send(200, dumps(_LIVE_POSITION))
            except Exception as e:
                traceback.print_exc()
                return self._send(200, dumps({"error": str(e)}))
                
        if path == "/api/live/exit":
            if not _LIVE_POSITION.get("active"):
                return self._send(200, dumps({"error": "No active position to exit."}))
            try:
                kc = Z.kite()
                _exit_live_position(kc, "Manual Square Off")
                return self._send(200, dumps(_LIVE_POSITION))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/order":
            sym  = body.get("symbol", "")
            side = body.get("side", "BUY").upper()
            otype = body.get("order_type", "MARKET").upper()
            trig  = body.get("trigger_price", None)
            try:
                kc = Z.kite()
                price = body.get("price", None)
                if otype == "SLM":
                    ktype = kc.ORDER_TYPE_SLM
                elif otype == "LIMIT":
                    ktype = kc.ORDER_TYPE_LIMIT
                else:
                    ktype = kc.ORDER_TYPE_MARKET
                kwargs = dict(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NSE,
                    tradingsymbol    = sym,
                    transaction_type = kc.TRANSACTION_TYPE_BUY if side=="BUY" else kc.TRANSACTION_TYPE_SELL,
                    quantity         = 1,
                    product          = kc.PRODUCT_MIS,
                    order_type       = ktype,
                )
                if otype == "SLM" and trig:
                    kwargs["trigger_price"] = float(trig)
                if otype == "LIMIT" and price:
                    kwargs["price"] = float(price)
                oid = kc.place_order(**kwargs)
                return self._send(200, dumps({"order_id": oid, "symbol": sym, "side": side}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/entry":
            sym    = body.get("symbol", "")
            side   = body.get("side", "BUY").upper()
            price  = body.get("price")
            sl     = body.get("sl")
            target = body.get("target")
            qty    = int(body.get("quantity", 1))
            if not sym or not price or not sl or not target:
                return self._send(200, dumps({"error": "symbol, price, sl, target required"}))
            try:
                kc = Z.kite()
                tt = kc.TRANSACTION_TYPE_BUY if side == "BUY" else kc.TRANSACTION_TYPE_SELL
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NSE,
                    tradingsymbol    = sym,
                    transaction_type = tt,
                    quantity         = qty,
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = float(price),
                )
                exit_side = "SELL" if side == "BUY" else "BUY"
                _BRACKET.pop(sym, None)
                threading.Thread(
                    target=_place_bracket_after_fill,
                    args=(kc, oid, sym, kc.EXCHANGE_NSE, sym, exit_side, qty,
                          kc.PRODUCT_MIS, float(sl), float(target)),
                    daemon=True
                ).start()
                return self._send(200, dumps({"order_id": oid, "symbol": sym, "side": side}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/order-status":
            # read-only status poll — used by the client to confirm an entry order
            # actually filled before it starts treating the trade as live for
            # SL/target purposes. Never places or modifies an order.
            oid = str(body.get("order_id", "")).strip()
            if not oid:
                return self._send(200, dumps({"error": "order_id required"}))
            try:
                kc = Z.kite()
                hist = kc.order_history(oid)
                last = hist[-1] if hist else {}
                return self._send(200, dumps({
                    "status": last.get("status", "UNKNOWN"),
                    "average_price": last.get("average_price"),
                }))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/square-off":
            sym = body.get("symbol", "").strip().upper()
            b = _BRACKET.get(sym)
            if not b or b["closed"]:
                return self._send(200, dumps({"error": "no open bracket for " + sym}))
            try:
                kc = Z.kite()
                _force_square_off(kc, sym, "MANUAL")
                return self._send(200, dumps({"squared_off": sym}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/option-entry":
            # OPTION mode: no options-chain subscription anywhere — equity ticks
            # (already streaming) are what drive entry/exit. This just buys the ATM
            # CE/PE at its current premium (fetched via REST, one-shot) — no resting
            # bracket, no polling. Exit is triggered later by /api/trading/option-exit
            # once the equity side hits its own SL/target.
            sym  = body.get("symbol", "").strip().upper()
            side = body.get("side", "BUY").upper()   # underlying equity signal direction
            if not sym:
                return self._send(200, dumps({"error": "symbol required"}))
            try:
                kc = Z.kite()
                stock_ltp = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]["last_price"]
                opt_type = "CE" if side == "BUY" else "PE"   # never a naked sell — always buy CE or PE
                opt = _resolve_option(kc, sym, stock_ltp, opt_type)
                if not opt:
                    return self._send(200, dumps({"error": f"no {opt_type} option found for {sym}"}))
                tradingsymbol = opt["tradingsymbol"]
                lot_size = opt["lot_size"]
                opt_ltp = kc.ltp(f"NFO:{tradingsymbol}")[f"NFO:{tradingsymbol}"]["last_price"]
                required = opt_ltp * lot_size
                margins = kc.margins("equity")
                available = margins.get("available", {}).get("live_balance", margins.get("net", 0))
                if available < required:
                    return self._send(200, dumps({
                        "error": f"Insufficient funds: need ₹{required:.2f}, available ₹{available:.2f}"
                    }))
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_BUY,   # always buy the option — no naked sells
                    quantity         = lot_size,
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = opt_ltp,
                )
                return self._send(200, dumps({
                    "order_id": oid, "symbol": sym, "tradingsymbol": tradingsymbol,
                    "strike": opt["strike"], "type": opt_type, "lot_size": lot_size,
                    "entry_premium": opt_ltp,
                }))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/option-exit":
            # Called once the underlying equity hits its own SL/target — closes the
            # option position at its current premium via a plain LIMIT sell.
            tradingsymbol = body.get("tradingsymbol", "").strip().upper()
            qty = int(body.get("quantity", 0))
            if not tradingsymbol or qty <= 0:
                return self._send(200, dumps({"error": "tradingsymbol, quantity required"}))
            try:
                kc = Z.kite()
                opt_ltp = kc.ltp(f"NFO:{tradingsymbol}")[f"NFO:{tradingsymbol}"]["last_price"]
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_SELL,  # closing a long — not a fresh short
                    quantity         = qty,
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = opt_ltp,
                )
                return self._send(200, dumps({"order_id": oid, "tradingsymbol": tradingsymbol, "exit_price": opt_ltp}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/trading/cancel":
            oid = body.get("order_id")
            if not oid:
                return self._send(200, dumps({"error": "order_id required"}))
            try:
                kc = Z.kite()
                kc.cancel_order(variety=kc.VARIETY_REGULAR, order_id=str(oid))
                return self._send(200, dumps({"cancelled": oid}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        if path == "/api/autotrader/pick":
            import autotrader as AT
            sym  = body.get("symbol", "").strip().upper()
            score= int(body.get("score", 0))
            ratio= float(body.get("ratio", 0))
            chg  = float(body.get("chgPct", 0))
            qty  = max(1, int(body.get("quantity", 1)))
            side = body.get("side", "BUY").upper()
            ltp  = float(body.get("ltp", 0))
            test = bool(body.get("test", False))
            if not sym:
                return self._send(200, dumps({"error": "symbol required"}))
            trader = AT.SELLER if side == "SELL" else AT.BUYER
            trader.pick  = {"symbol": sym, "score": score, "ratio": ratio, "chgPct": chg,
                            "quantity": qty, "side": side, "ltp": ltp}
            trader.state = "locked"
            trader._log(f"{'[TEST] ' if test else ''}PICK [{side}]: {sym} score={score}/7 ratio={ratio} chg={chg:+.2f}% qty={qty}")
            delay = 10 if test else None
            t = threading.Thread(target=trader._wait_and_order, kwargs={"delay": delay}, daemon=True)
            t.start()
            return self._send(200, dumps({"buy": AT.BUYER.status(), "sell": AT.SELLER.status()}))
        if path == "/api/autotest/record":
            import auto_test as AXT, datetime as _dt
            ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
            result = AXT.save_entry(str(ist.date()), body)
            return self._send(200, dumps(result))
        if path == "/api/scanner/snapshot":
            import data_store as DS, datetime as _dt
            ist = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
            date_str = str(ist.date())
            snap_type = body.get("type", "premarket")
            if snap_type == "session":
                session_id = body.get("session_id", ist.strftime("%H-%M-%S"))
                result = DS.save_session(date_str, session_id, body)
            else:
                result = DS.save_premarket(date_str, body)
            return self._send(200, dumps(result))
        if path == "/api/options/autotrader/start":
            capital = float(body.get("capital", 40000.0))
            mode = body.get("mode", "paper").strip().lower()
            vol_pcr_mode = body.get("vol_pcr_mode", "paper").strip().lower()
            lot_size_mode = body.get("lot_size_mode", "auto").strip().lower()
            fixed_lots = int(body.get("fixed_lots", 1))
            options_trader.start(capital=capital, mode=mode, lot_size_mode=lot_size_mode, fixed_lots=fixed_lots, vol_pcr_mode=vol_pcr_mode)
            return self._send(200, dumps(options_trader.status()))
        if path == "/api/options/autotrader/stop":
            options_trader.stop()
            return self._send(200, dumps(options_trader.status()))
        if path == "/api/futures/run":
            try:
                out = run_futures_algo(body)
                self._send(200, dumps(out))
            except Exception as e:
                traceback.print_exc()
                self._send(500, dumps({"error": f"{type(e).__name__}: {e}"}))
            return
        if path == "/api/options/run":
            try:
                out = run_options_algo(body)
                self._send(200, dumps(out))
            except Exception as e:
                traceback.print_exc()
                self._send(500, dumps({"error": f"{type(e).__name__}: {e}"}))
            return
        if path != "/api/run":
            return self._send(404, dumps({"error": "not found"}))
        try:
            out = run_algo(body)
            self._send(200, dumps(out))
        except Exception as e:
            traceback.print_exc()
            self._send(500, dumps({"error": f"{type(e).__name__}: {e}"}))

    def _dummy_stream(self):
        import random, time as _time
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        sym = qs.get("symbol", ["RELIANCE"])[0]
        try:
            ltp = float(qs.get("price", [0])[0])
        except (ValueError, IndexError):
            ltp = 0
        if not ltp:
            ltp = random.uniform(500, 3000)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for _ in range(420):  # ~7 min at 1 tick/sec
                buy  = random.randint(10000, 800000)
                sell = random.randint(10000, 800000)
                # price moves in direction of buy/sell imbalance
                imbalance = (buy - sell) / (buy + sell)   # -1..+1
                ltp *= (1 + imbalance * 0.0006)
                eip  = round(ltp, 2)
                tick = dumps({"symbol": sym, "ltp": round(ltp, 2),
                              "buy": buy, "sell": sell, "eip": eip})
                self.wfile.write(f"data: {tick}\n\n".encode())
                self.wfile.flush()
                _time.sleep(1)
        except Exception:
            pass

    def log_message(self, *a):
        pass


def _auto_record():
    """Hands-off: once a day after ~16:00 IST, compute + record the daily pick so the
    track record builds even on days nobody opens the app."""
    while True:
        try:
            ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            today = str(ist.date())
            if ist.hour >= 16:
                for mk in ("IN", "US"):
                    c = _REC_CACHE.get(mk)
                    if not c or c["date"] != today:
                        _REC_CACHE[mk] = {"date": today, "data": REC.daily_pick(market=mk)}
                        print(f"[auto-record] {mk} pick saved for {today}", flush=True)
        except Exception as e:
            print(f"[auto-record] {e}", flush=True)
        time.sleep(1800)   # check every 30 min


def _news_watchlist(market):
    """Symbols the radar watches: today's cached picks, else the top of the universe."""
    c = _REC_CACHE.get(market)
    picks = (c or {}).get("data", {}).get("picks") if c else None
    if picks:
        return [p["symbol"] for p in picks]
    uni = REC.MARKETS.get(market, REC.MARKETS["IN"])["universe"]
    return uni[:config.NEWS_WATCH_FALLBACK]


def _news_radar_loop():
    """Hands-off: every ~60s pull fresh headlines for the watched names in each
    market and classify them into buy/sell/volatility flashes for the UI."""
    while True:
        try:
            for mk in ("IN", "US"):
                m = REC.MARKETS.get(mk, REC.MARKETS["IN"])
                NF.get_radar(mk).poll(_news_watchlist(mk), bench_query=m["bench_name"])
        except Exception as e:
            print(f"[news-radar] {e}", flush=True)
        time.sleep(max(20, config.NEWS_POLL_SEC))


threading.Thread(target=_auto_record, daemon=True).start()
threading.Thread(target=_news_radar_loop, daemon=True).start()
threading.Thread(target=_live_position_monitor_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")   # 0.0.0.0 so Railway can route to it
    print(f"stockflow UI on {host}:{port}   (source={config.SOURCE})")
    ThreadingHTTPServer((host, port), H).serve_forever()
