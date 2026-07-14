"""Nifty Options Auto-Trader — Live and Paper trading for the Pullback Strategy.

Polls Nifty Spot Index at 1-second intervals, compiles 1-minute candles,
calculates 15-EMA + ATR, manages the multi-directional state machine,
and triggers option positions using actual premium quotes from Zerodha.
"""
from __future__ import annotations
import datetime as dt
import threading
import time
import math
import logging
import pandas as pd
import pytz
import config
import zerodha as Z

log = logging.getLogger("options_autotrader")

class OptionsAutoTrader:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.mode = "paper"  # "paper" | "live"
        self.capital = 40000.0
        self.lot_size_mode = "auto" # "auto" | "fixed"
        self.fixed_lots = 1
        self.state = "idle"  # idle | warmup | scanning | in-trade | error
        self.logs = []
        self.candles = []    # list of dicts: {"open", "high", "low", "close", "atr", "nifty_ema", "date"}
        self.active_trade = None  # None or dict of active trade parameters
        self.completed_trades = []
        self.thread = None
        self._stop = False
        self.nifty_open = None

    def _ist(self):
        return dt.datetime.now(pytz.timezone("Asia/Kolkata"))

    def _log(self, msg: str):
        ts = self._ist().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 300:
                self.logs.pop(0)
        log.info(entry)

    def start(self, capital=40000.0, mode="paper", lot_size_mode="auto", fixed_lots=1):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.mode = mode.lower()
            self.capital = float(capital)
            self.lot_size_mode = lot_size_mode.lower()
            self.fixed_lots = int(fixed_lots)
            self.state = "warmup"
            self._stop = False
            self.candles = []
            self.active_trade = None
            self.nifty_open = None
            self.logs = []
            lot_mode_str = "AUTO (Capital-based)" if self.lot_size_mode == "auto" else f"FIXED ({self.fixed_lots} lots)"
            self._log(f"Starting Options Auto-Trader in {self.mode.upper()} mode with Rs. {self.capital} capital (Lot Sizing: {lot_mode_str})...")
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            if not self.running:
                return False
            self._stop = True
            self.running = False
            self.state = "idle"
            self._log("Stopping Options Auto-Trader...")
            return True

    def _warmup(self, kc):
        """Fetch the last 2 hours of Nifty Spot candles to warm up EMA and ATR."""
        self._log("Fetching warmup historical Nifty index candles...")
        imap = Z.instrument_map(kc)
        tok = imap.get("NIFTY 50")
        if not tok:
            raise RuntimeError("Could not find Nifty 50 instrument token.")
        
        to_d = self._ist().replace(tzinfo=None)
        from_d = to_d - dt.timedelta(hours=4)
        
        rows = kc.historical_data(tok, from_d, to_d, "minute")
        if not rows:
            self._log("Warning: No warmup data returned. Indicators will build from scratch.")
            return

        self._log(f"Warmup loaded {len(rows)} index candles.")
        
        # Accumulate candles
        temp_candles = []
        for r in rows:
            ts = r["date"].astimezone(pytz.timezone("Asia/Kolkata")) if r["date"].tzinfo else r["date"]
            temp_candles.append({
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "date": ts
            })

        # Recalculate indicators (EMA 15, ATR 14)
        closes = pd.Series([c["close"] for c in temp_candles])
        ema_series = closes.ewm(span=15, adjust=False).mean().tolist()
        
        # ATR Calculation
        tr_history = []
        prev_close = None
        for i, c in enumerate(temp_candles):
            high, low, close = c["high"], c["low"], c["close"]
            if prev_close is None:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            prev_close = close
            tr_history.append(tr)
            if len(tr_history) > 14:
                tr_history.pop(0)
            
            c["atr"] = sum(tr_history) / len(tr_history) if len(tr_history) >= 7 else (high - low)
            c["nifty_ema"] = ema_series[i]

        self.candles = temp_candles
        # Grab Nifty session open from today's first candle
        today_date = self._ist().date()
        today_candles = [c for c in temp_candles if c["date"].date() == today_date]
        if today_candles:
            self.nifty_open = today_candles[0]["open"]
            self._log(f"Locked Nifty Session Open for today: {self.nifty_open}")
        else:
            # Fallback if starting before market open
            self._log("Starting before session open or no today's candles. Will lock open price on first tick.")

    def _loop(self):
        try:
            kc = Z.kite()
            self._warmup(kc)
            self.state = "scanning"
            self._log("Warm-up complete. Scanning Nifty Spot index for pullback signals.")
        except Exception as e:
            self.state = "error"
            self._log(f"Fatal Startup Error: {e}")
            self.running = False
            return

        # State machine stages
        l_peak = None
        l_trough = None
        l_peak_atr = None
        l_stage = 1
        
        s_trough = None
        s_peak = None
        s_trough_atr = None
        s_stage = 1

        # Track tick accumulation for live 1-minute candle
        current_minute = None
        live_open = None
        live_high = None
        live_low = None
        live_close = None

        while not self._stop:
            t0 = time.time()
            try:
                # Combine Spot and Option quotes to reduce request count
                symbols_to_quote = ["NSE:NIFTY 50"]
                with self.lock:
                    if self.active_trade and self.mode == "live" and self.active_trade.get("tradingsymbol"):
                        symbols_to_quote.append(f"NFO:{self.active_trade['tradingsymbol']}")
                
                q = kc.quote(symbols_to_quote)
                d_q = q.get("NSE:NIFTY 50")
                if not d_q:
                    time.sleep(1)
                    continue

                ltp = float(d_q.get("last_price") or 0)
                if ltp <= 0:
                    time.sleep(1)
                    continue

                # Set session open price if missing
                if self.nifty_open is None:
                    self.nifty_open = ltp
                    self._log(f"First Nifty Spot Tick observed. Session Open locked at Rs. {self.nifty_open}")

                ist = self._ist()
                time_str = ist.strftime("%H:%M")
                minute_key = ist.strftime("%Y-%m-%d %H:%M")

                # Accumulate 1-minute candle
                if current_minute != minute_key:
                    # Minute boundary crossed! Finalize previous candle
                    if current_minute is not None and live_close is not None:
                        new_candle = {
                            "open": live_open,
                            "high": live_high,
                            "low": live_low,
                            "close": live_close,
                            "date": ist - dt.timedelta(minutes=1)
                        }
                        
                        # Recalculate indicators
                        self.candles.append(new_candle)
                        if len(self.candles) > 300:
                            self.candles.pop(0)

                        closes = pd.Series([c["close"] for c in self.candles])
                        ema_val = float(closes.ewm(span=15, adjust=False).mean().iloc[-1])
                        
                        # Calculate ATR
                        tr_history = []
                        prev = None
                        for c in self.candles[-15:]:
                            h, l, cl = c["high"], c["low"], c["close"]
                            if prev is None:
                                tr = h - l
                            else:
                                tr = max(h - l, abs(h - prev), abs(l - prev))
                            prev = cl
                            tr_history.append(tr)
                        atr_val = sum(tr_history[-14:]) / len(tr_history[-14:]) if len(tr_history) >= 7 else (live_high - live_low)
                        
                        new_candle["atr"] = atr_val
                        new_candle["nifty_ema"] = ema_val

                        # Run state machine on completed candle
                        high = live_high
                        low = live_low
                        close = live_close
                        atr = atr_val
                        nifty_ema = ema_val

                        is_nifty_above_ema = close > nifty_ema
                        is_nifty_below_ema = close < nifty_ema
                        is_nifty_green_today = close > self.nifty_open
                        is_nifty_red_today = close < self.nifty_open
                        is_valid_time = ("10:00" <= time_str < "11:00") or ("14:00" <= time_str < "15:30")

                        # LONG STATE MACHINE
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
                            if high >= bounce_level and not self.active_trade:
                                if is_valid_time and is_nifty_above_ema and is_nifty_green_today:
                                    # Trigger LONG CE Trade!
                                    self._enter_position(kc, "BUY CALL (CE)", bounce_level, atr_val, ema_val)
                                    l_peak = None
                                    l_trough = None
                                    l_stage = 1

                        # SHORT STATE MACHINE
                        if not self.active_trade:
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
                                if low <= short_trigger_level and not self.active_trade:
                                    if is_valid_time and is_nifty_below_ema and is_nifty_red_today:
                                        # Trigger SHORT PE Trade!
                                        self._enter_position(kc, "BUY PUT (PE)", short_trigger_level, atr_val, ema_val)
                                        s_trough = None
                                        s_peak = None
                                        s_stage = 1

                    # Initialize next live 1-minute candle
                    current_minute = minute_key
                    live_open = ltp
                    live_high = ltp
                    live_low = ltp
                    live_close = ltp
                else:
                    # Accumulate ticks
                    live_high = max(live_high, ltp)
                    live_low = min(live_low, ltp)
                    live_close = ltp

                # If in position, manage exits at 1-second resolution
                if self.active_trade:
                    self._manage_active_position(kc, ltp, quote_data=q)

            except Exception as e:
                self._log(f"Loop error: {e}")
            
            # Poll every second
            time.sleep(max(0.1, 1.0 - (time.time() - t0)))

    def _enter_position(self, kc, side, entry_spot, atr, ema):
        strike = int(round(entry_spot / 50.0) * 50.0)
        opt_type = "CE" if "CALL" in side else "PE"
        today_date = self._ist().date()
        
        # Calculate dynamic Tuesday expiry
        days_until_tuesday = (1 - today_date.weekday() + 7) % 7
        expiry_date = today_date + dt.timedelta(days=days_until_tuesday)
        expiry_str = expiry_date.strftime("%d %b").upper()
        symbol_str = f"NIFTY {expiry_str} {strike} {opt_type}"

        self._log(f"SIGNAL FIRED: {side} at Spot ₹{entry_spot:.2f} (ATR={atr:.2f})")
        self.state = "in-trade"

        # Calculate target & stop-loss levels on spot index
        # Standard: Target = 2.0 * ATR (min 0.3%), SL = 1.0 * ATR (min 0.15%)
        raw_sl_pct = atr / entry_spot
        raw_target_pct = (2.0 * atr) / entry_spot
        actual_sl_pct = max(raw_sl_pct, 0.0015)
        actual_target_pct = max(raw_target_pct, 0.0030)

        if opt_type == "CE":
            spot_sl = entry_spot * (1 - actual_sl_pct)
            spot_target = entry_spot * (1 + actual_target_pct)
        else:
            spot_sl = entry_spot * (1 + actual_sl_pct)
            spot_target = entry_spot * (1 - actual_target_pct)

        # Tuesday premium decay decay estimation
        days_to_expiry = days_until_tuesday
        decay_factor = 0.005 if days_to_expiry <= 1 else 0.007 if days_to_expiry == 2 else 0.010 if days_to_expiry == 3 else 0.013 if days_to_expiry <= 5 else 0.016
        fallback_premium = entry_spot * decay_factor

        entry_premium = fallback_premium
        tradingsymbol = ""
        lot_size = 75
        
        if self.lot_size_mode == "fixed":
            lots = self.fixed_lots
        else:
            lot_cost = entry_premium * lot_size
            lots = math.floor(self.capital / lot_cost) if lot_cost > 0 else 0
            if lots == 0:
                lots = 1
        
        if self.mode == "live":
            try:
                opt_token = Z.get_option_token(kc, "NIFTY", expiry_date, strike, opt_type)
                if not opt_token:
                    raise ValueError(f"No active option token found for Nifty {expiry_str} {strike} {opt_type}")
                
                # Fetch actual contract trading symbol and lot size from instruments cache
                insts = Z.get_nfo_instruments(kc)
                matched = next((i for i in insts if int(i.get("instrument_token") or 0) == opt_token), None)
                if matched:
                    tradingsymbol = matched["tradingsymbol"]
                    lot_size = int(matched.get("lot_size") or 75)
                
                # Fetch current option LTP
                quote = kc.quote([f"NFO:{tradingsymbol}"])
                opt_q = quote.get(f"NFO:{tradingsymbol}")
                if opt_q:
                    entry_premium = float(opt_q.get("last_price") or entry_premium)

                if self.lot_size_mode == "fixed":
                    lots = self.fixed_lots
                else:
                    lot_cost = entry_premium * lot_size
                    lots = math.floor(self.capital / lot_cost) if lot_cost > 0 else 0
                
                required_capital = lots * entry_premium * lot_size
                if self.capital < required_capital:
                    raise ValueError(f"Insufficient capital. Required: Rs. {required_capital}, Available: Rs. {self.capital}")
                if lots == 0:
                    raise ValueError(f"Trade lot size evaluated to 0. Mode: {self.lot_size_mode}")

                # To simulate a market buy with protection: set limit price 2% above current premium
                limit_price = round(entry_premium * 1.02, 1)
                
                self._log(f"[LIVE] Placing LIMIT BUY order (with 2% market protection) at Rs. {limit_price} for {lots} lots ({lots*lot_size} qty) of {tradingsymbol}...")
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = tradingsymbol,
                    transaction_type = kc.TRANSACTION_TYPE_BUY,
                    quantity         = lots * lot_size,
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = limit_price,
                )
                self._log(f"[LIVE] Order placed successfully. ID: {oid}")
                
                # Poll for fill
                self._log("[LIVE] Polling order book for average fill price...")
                time.sleep(1)
                orders = kc.orders()
                o = next((x for x in orders if str(x["order_id"]) == str(oid)), None)
                if o and o["status"] == "COMPLETE":
                    entry_premium = float(o["average_price"])
                    self._log(f"[LIVE] FILLED at premium Rs. {entry_premium:.2f}")
                else:
                    self._log(f"[LIVE] Warning: Order not marked complete yet. Using quoted LTP Rs. {entry_premium:.2f}")

            except Exception as e:
                self.state = "error"
                self._log(f"[LIVE ERROR] Order Entry Failed: {e}")
                self.active_trade = None
                return
        else:
            # Paper Trading Fills
            lot_cost = entry_premium * lot_size
            lots = math.floor(self.capital / lot_cost) if lot_cost > 0 else 0
            self._log(f"[PAPER] Simulating Buy {lots} lots CE/PE @ Rs. {entry_premium:.2f} premium")

        with self.lock:
            self.active_trade = {
                "side": side,
                "symbol": symbol_str,
                "tradingsymbol": tradingsymbol,
                "entry_time": self._ist().strftime("%H:%M"),
                "entry_spot": entry_spot,
                "spot_sl": spot_sl,
                "spot_target": spot_target,
                "current_sl": spot_sl,  # Will trail to entry at halfway
                "reached_halfway": False,
                "entry_premium": entry_premium,
                "current_premium": entry_premium,
                "lots": lots,
                "lot_size": lot_size,
                "started_at": time.time()
            }

    def _manage_active_position(self, kc, spot_ltp, quote_data=None):
        t = self.active_trade
        if not t:
            return

        side = t["side"]
        entry_spot = t["entry_spot"]
        target = t["spot_target"]
        sl = t["spot_sl"]
        current_sl = t["current_sl"]
        reached_halfway = t["reached_halfway"]
        is_call = "CALL" in side

        # Update option LTP for floating P&L display
        if self.mode == "live" and t["tradingsymbol"]:
            try:
                opt_key = f"NFO:{t['tradingsymbol']}"
                if quote_data and opt_key in quote_data:
                    opt_q = quote_data[opt_key]
                else:
                    quote = kc.quote([opt_key])
                    opt_q = quote.get(opt_key)
                
                if opt_q:
                    t["current_premium"] = float(opt_q.get("last_price") or t["current_premium"])
            except Exception:
                pass
        else:
            # Paper P&L estimation: change in spot price * delta
            spot_change = (spot_ltp - entry_spot) if is_call else (entry_spot - spot_ltp)
            t["current_premium"] = max(1.0, t["entry_premium"] + (spot_change * 0.50))

        # Check time-decay timeout (45 minutes max hold)
        elapsed_mins = (time.time() - t["started_at"]) / 60.0
        if elapsed_mins >= 45.0:
            self._log(f"Time-Decay Trigger: Duration exceeded 45 minutes ({elapsed_mins:.1f}m). Exiting trade.")
            self._exit_position(kc, "TIMEOUT", spot_ltp)
            return

        # Check trailing breakeven condition (30% progress point)
        if not reached_halfway:
            if is_call:
                trail_level = entry_spot + 0.3 * (target - entry_spot)
                if spot_ltp >= trail_level:
                    t["reached_halfway"] = True
                    t["current_sl"] = entry_spot
                    self._log(f"Trail Trigger: Spot hit 30% progress Rs. {spot_ltp:.2f}. Trailing Stop-Loss to entry Rs. {entry_spot:.2f}")
            else:
                trail_level = entry_spot - 0.3 * (entry_spot - target)
                if spot_ltp <= trail_level:
                    t["reached_halfway"] = True
                    t["current_sl"] = entry_spot
                    self._log(f"Trail Trigger: Spot hit 30% progress Rs. {spot_ltp:.2f}. Trailing Stop-Loss to entry Rs. {entry_spot:.2f}")

        # Check exit triggers
        exit_triggered = False
        verdict = "OPEN"
        exit_spot = spot_ltp

        if is_call:
            if spot_ltp <= current_sl:
                exit_triggered = True
                verdict = "BREAKEVEN" if reached_halfway else "LOSS"
                exit_spot = current_sl
            elif spot_ltp >= target:
                exit_triggered = True
                verdict = "WIN"
                exit_spot = target
        else:
            if spot_ltp >= current_sl:
                exit_triggered = True
                verdict = "BREAKEVEN" if reached_halfway else "LOSS"
                exit_spot = current_sl
            elif spot_ltp <= target:
                exit_triggered = True
                verdict = "WIN"
                exit_spot = target

        if exit_triggered:
            self._exit_position(kc, verdict, exit_spot)

    def _exit_position(self, kc, verdict, exit_spot):
        t = self.active_trade
        if not t:
            return

        exit_premium = t["current_premium"]
        total_shares = t["lots"] * t["lot_size"]
        options_brokerage = 40.0
        options_slippage = 2.0

        if self.mode == "live" and t["tradingsymbol"]:
            # To simulate a market sell with protection: set limit price 2% below current premium
            limit_price = round(exit_premium * 0.98, 1)
            
            self._log(f"[LIVE] Placing LIMIT SELL order (with 2% market protection) at Rs. {limit_price} to close {t['lots']} lots {t['tradingsymbol']}...")
            try:
                oid = kc.place_order(
                    variety          = kc.VARIETY_REGULAR,
                    exchange         = kc.EXCHANGE_NFO,
                    tradingsymbol    = t["tradingsymbol"],
                    transaction_type = kc.TRANSACTION_TYPE_SELL,
                    quantity         = t["lots"] * t["lot_size"],
                    product          = kc.PRODUCT_MIS,
                    order_type       = kc.ORDER_TYPE_LIMIT,
                    price            = limit_price,
                )
                self._log(f"[LIVE] Sell order placed: {oid}")
                
                # Poll fill
                time.sleep(1)
                orders = kc.orders()
                o = next((x for x in orders if str(x["order_id"]) == str(oid)), None)
                if o and o["status"] == "COMPLETE":
                    exit_premium = float(o["average_price"])
                    self._log(f"[LIVE] FILLED close @ Rs. {exit_premium:.2f} premium")
            except Exception as e:
                self.state = "error"
                self._log(f"[LIVE ERROR] Exit Order Failed: {e} — EXIT POSITION MANUALLY!")

        # Compute P&L
        if verdict == "BREAKEVEN":
            pnl = - (t["lots"] * options_brokerage) - (options_slippage * total_shares)
        else:
            pnl_gross = (exit_premium - t["entry_premium"]) * total_shares
            pnl = pnl_gross - (t["lots"] * options_brokerage) - (options_slippage * total_shares)

        completed_trade = {
            "date": self._ist().strftime("%Y-%m-%d"),
            "symbol": t["symbol"],
            "side": t["side"],
            "entry_time": t["entry_time"],
            "exit_time": self._ist().strftime("%H:%M"),
            "duration": f"{int((time.time() - t['started_at']) / 60)}m",
            "entry_spot": t["entry_spot"],
            "exit_spot": exit_spot,
            "spot_target": t["spot_target"],
            "spot_sl": t["spot_sl"],
            "entry_premium": t["entry_premium"],
            "exit_premium": exit_premium,
            "result": verdict,
            "pnl": round(pnl),
            "lots": t["lots"]
        }

        with self.lock:
            self.completed_trades.append(completed_trade)
            self.active_trade = None
            self.state = "scanning"

        self._log(f"TRADE CLOSED: {completed_trade['side']} -> {verdict} | P&L: Rs. {completed_trade['pnl']:+}")

    def status(self):
        with self.lock:
            floating_pnl = 0
            if self.active_trade:
                t = self.active_trade
                total_shares = t["lots"] * t["lot_size"]
                options_brokerage = 40.0
                options_slippage = 2.0
                pnl_gross = (t["current_premium"] - t["entry_premium"]) * total_shares
                floating_pnl = pnl_gross - (t["lots"] * options_brokerage) - (options_slippage * total_shares)

            return {
                "running": self.running,
                "mode": self.mode,
                "capital": self.capital,
                "state": self.state,
                "logs": self.logs[-30:],
                "active_trade": {
                    "symbol": t["symbol"] if self.active_trade else "-",
                    "side": t["side"] if self.active_trade else "-",
                    "entry_time": t["entry_time"] if self.active_trade else "-",
                    "entry_spot": round(t["entry_spot"], 2) if self.active_trade else 0,
                    "spot_target": round(t["spot_target"], 2) if self.active_trade else 0,
                    "current_sl": round(t["current_sl"], 2) if self.active_trade else 0,
                    "entry_premium": round(t["entry_premium"], 2) if self.active_trade else 0,
                    "current_premium": round(t["current_premium"], 2) if self.active_trade else 0,
                    "lots": t["lots"] if self.active_trade else 0,
                    "pnl": round(floating_pnl) if self.active_trade else 0
                } if self.active_trade else None,
                "completed_trades": self.completed_trades
            }
