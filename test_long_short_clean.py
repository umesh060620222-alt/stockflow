import datetime
import yfinance as yf
import pandas as pd

# 20 highly liquid stocks
SYMS = [
    "ICICIBANK.NS", "AXISBANK.NS", "SBIN.NS", "HCLTECH.NS", "WIPRO.NS",
    "TECHM.NS", "DRREDDY.NS", "CIPLA.NS", "SUNPHARMA.NS", "TATAMOTORS.NS",
    "HDFCBANK.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "HINDALCO.NS", "ADANIPORTS.NS",
    "JSWSTEEL.NS", "INDUSINDBK.NS", "SHRIRAMFIN.NS", "TATACONSUM.NS", "COALINDIA.NS"
]

PULLBACK_1 = 2.5      # 2.5x ATR
PULLBACK_2 = 0.7      # 0.7x ATR
SL_ATR_MULT = 1.5     # Stop loss: 1.5x ATR
TARGET_ATR_MULT = 3.0 # Target: 3.0x ATR
VOL_MULT = 1.5        # Volume surge multiplier
FRICTION = 0.0016     # 0.16% total friction (brokerage + slippage)

MIN_SL_PCT = 0.004    # Minimum Stop Loss of 0.4%
MIN_TARGET_PCT = 0.008 # Minimum Target of 0.8%

def simulate_long_short_clean(candles, nifty_close, nifty_ema):
    # Calculate ATR(14)
    prev_close = None
    tr_history = []
    
    for c in candles:
        high = float(c["high"])
        low = float(c["low"])
        close = float(c["close"])
        
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        prev_close = close
        
        tr_history.append(tr)
        if len(tr_history) > 14:
            tr_history.pop(0)
            
        c["atr"] = sum(tr_history) / len(tr_history) if len(tr_history) >= 7 else (high - low)

    # Long state machine variables
    l_peak = None
    l_trough = None
    l_peak_atr = None
    l_stage = 1
    
    # Short state machine variables
    s_trough = None
    s_peak = None
    s_trough_atr = None
    s_stage = 1
    
    vol_history = []
    trades = []
    locked_until_idx = -1
    
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        vol = float(c["volume"])
        atr = float(c["atr"])
        ts = c["date"]
        
        if i <= locked_until_idx:
            vol_history.append(vol)
            if len(vol_history) > 12:
                vol_history.pop(0)
            continue
            
        # Get Nifty status
        is_nifty_bullish = False
        is_nifty_bearish = False
        if nifty_close is not None and nifty_ema is not None:
            nifty_p = nifty_close.get(ts)
            nifty_e = nifty_ema.get(ts)
            if nifty_p is None and not nifty_close.empty:
                closest_ts = min(nifty_close.index, key=lambda x: abs((x - ts).total_seconds()))
                nifty_p = nifty_close.loc[closest_ts]
                nifty_e = nifty_ema.loc[closest_ts]
            if nifty_p is not None and nifty_e is not None:
                is_nifty_bullish = nifty_p > nifty_e
                is_nifty_bearish = nifty_p < nifty_e
                
        # Common filters
        time_str = ts.strftime("%H:%M")
        is_valid_time = ("10:00" <= time_str < "11:00") or ("14:00" <= time_str < "15:30")
        
        if len(vol_history) >= 3:
            vol_avg = sum(vol_history) / len(vol_history)
        else:
            vol_avg = 0
        is_valid_vol = (vol > VOL_MULT * vol_avg) if vol_avg > 0 else True
        
        # ------------------------------------
        # LONG SETUP STATE MACHINE
        # ------------------------------------
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
                drop_required = PULLBACK_1 * (l_peak_atr if l_peak_atr else atr)
                if l_trough <= l_peak - drop_required:
                    l_stage = 3
        elif l_stage == 3:
            if low < l_trough:
                l_trough = low
            bounce_required = PULLBACK_2 * atr
            bounce_level = l_trough + bounce_required
            if high >= bounce_level:
                if is_valid_time and is_valid_vol and is_nifty_bullish:
                    entry = bounce_level
                    raw_sl_pct = (SL_ATR_MULT * atr) / entry
                    raw_target_pct = (TARGET_ATR_MULT * atr) / entry
                    actual_sl_pct = max(raw_sl_pct, MIN_SL_PCT)
                    actual_target_pct = max(raw_target_pct, MIN_TARGET_PCT)
                    
                    sl = entry * (1 - actual_sl_pct)
                    target = entry * (1 + actual_target_pct)
                    
                    # Evaluate exits
                    trade_result = "OPEN"
                    exit_price_val = None
                    
                    # Check same-candle exit first (pessimistic check)
                    if low <= sl:
                        trade_result = "LOSS"
                        exit_price_val = sl
                        locked_until_idx = i
                    elif high >= target:
                        trade_result = "WIN"
                        exit_price_val = target
                        locked_until_idx = i
                    else:
                        # Check subsequent candles
                        reached_halfway = False
                        current_sl = sl
                        for idx_w, w in enumerate(candles[i+1:], start=i+1):
                            w_low = float(w["low"])
                            w_high = float(w["high"])
                            
                            halfway_level = entry + 0.5 * (target - entry)
                            if w_high >= halfway_level:
                                reached_halfway = True
                                current_sl = entry
                                
                            if w_low <= current_sl:
                                trade_result = "LOSS" if not reached_halfway else "BREAKEVEN"
                                exit_price_val = current_sl
                                locked_until_idx = idx_w
                                break
                            if w_high >= target:
                                trade_result = "WIN"
                                exit_price_val = target
                                locked_until_idx = idx_w
                                break
                                
                    pnl_pct = (actual_target_pct - FRICTION) if trade_result == "WIN" else ((-actual_sl_pct - FRICTION) if trade_result == "LOSS" else (-FRICTION if trade_result == "BREAKEVEN" else 0.0))
                    trades.append({
                        "sym": c["symbol"],
                        "side": "LONG",
                        "entry": entry,
                        "exit": exit_price_val,
                        "result": trade_result,
                        "entry_time": time_str,
                        "pnl": 100000 * pnl_pct if trade_result in ("WIN", "LOSS", "BREAKEVEN") else 0.0
                    })
                    long_triggered = True
                    l_peak = None
                    l_trough = None
                    l_peak_atr = None
                    l_stage = 1

        # ------------------------------------
        # SHORT SETUP STATE MACHINE
        # ------------------------------------
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
                    rally_required = PULLBACK_1 * (s_trough_atr if s_trough_atr else atr)
                    if s_peak >= s_trough + rally_required:
                        s_stage = 3
            elif s_stage == 3:
                if high > s_peak:
                    s_peak = high
                drop_required = PULLBACK_2 * atr
                short_trigger_level = s_peak - drop_required
                if low <= short_trigger_level:
                    if is_valid_time and is_valid_vol and is_nifty_bearish:
                        entry = short_trigger_level
                        raw_sl_pct = (SL_ATR_MULT * atr) / entry
                        raw_target_pct = (TARGET_ATR_MULT * atr) / entry
                        actual_sl_pct = max(raw_sl_pct, MIN_SL_PCT)
                        actual_target_pct = max(raw_target_pct, MIN_TARGET_PCT)
                        
                        sl = entry * (1 + actual_sl_pct)
                        target = entry * (1 - actual_target_pct)
                        
                        trade_result = "OPEN"
                        exit_price_val = None
                        
                        # Check same-candle exit first
                        if high >= sl:
                            trade_result = "LOSS"
                            exit_price_val = sl
                            locked_until_idx = i
                        elif low <= target:
                            trade_result = "WIN"
                            exit_price_val = target
                            locked_until_idx = i
                        else:
                            # Check subsequent candles
                            reached_halfway = False
                            current_sl = sl
                            for idx_w, w in enumerate(candles[i+1:], start=i+1):
                                w_low = float(w["low"])
                                w_high = float(w["high"])
                                
                                halfway_level = entry - 0.5 * (entry - target)
                                if w_low <= halfway_level:
                                    reached_halfway = True
                                    current_sl = entry
                                    
                                if w_high >= current_sl:
                                    trade_result = "LOSS" if not reached_halfway else "BREAKEVEN"
                                    exit_price_val = current_sl
                                    locked_until_idx = idx_w
                                    break
                                if w_low <= target:
                                    trade_result = "WIN"
                                    exit_price_val = target
                                    locked_until_idx = idx_w
                                    break
                                    
                        pnl_pct = (actual_target_pct - FRICTION) if trade_result == "WIN" else ((-actual_sl_pct - FRICTION) if trade_result == "LOSS" else (-FRICTION if trade_result == "BREAKEVEN" else 0.0))
                        trades.append({
                            "sym": c["symbol"],
                            "side": "SHORT",
                            "entry": entry,
                            "exit": exit_price_val,
                            "result": trade_result,
                            "entry_time": time_str,
                            "pnl": 100000 * pnl_pct if trade_result in ("WIN", "LOSS", "BREAKEVEN") else 0.0
                        })
                        s_trough = None
                        s_peak = None
                        s_trough_atr = None
                        s_stage = 1

        vol_history.append(vol)
        if len(vol_history) > 12:
            vol_history.pop(0)
            
    return trades

def main():
    print("Downloading 7 calendar days of 1-minute historical data (including Nifty)...")
    raw = yf.download(SYMS + ["^NSEI"], period="7d", interval="1m", group_by="ticker", progress=False)
    
    ticker = "ICICIBANK.NS"
    df_sample = raw[ticker].dropna(how="all")
    if df_sample.index.tz is None:
        df_sample.index = df_sample.index.tz_localize("UTC")
    df_sample.index = df_sample.index.tz_convert("Asia/Kolkata")
    
    dates = sorted(list(set(df_sample.index.date)))
    print(f"Trading days found: {[str(d) for d in dates]}")
    
    nifty_df = raw["^NSEI"].dropna(how="all").copy()
    if nifty_df.index.tz is None:
        nifty_df.index = nifty_df.index.tz_localize("UTC")
    nifty_df.index = nifty_df.index.tz_convert("Asia/Kolkata")
    
    daily_summaries = []
    cumulative_pnl = 0.0
    
    for d in dates:
        nifty_session = nifty_df[nifty_df.index.date == d]
        if nifty_session.empty:
            continue
        nifty_close = nifty_session["Close"].astype(float)
        nifty_ema = nifty_close.ewm(span=15, adjust=False).mean()
        
        all_trades = []
        for sym in SYMS:
            try:
                df = raw[sym].copy()
                df = df.dropna(how="all")
                if df.empty:
                    continue
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                df.index = df.index.tz_convert("Asia/Kolkata")
                
                df_session = df[df.index.date == d]
                df_session.columns = df_session.columns.str.lower()
                df_session = df_session[["open", "high", "low", "close", "volume"]]
                df_session = df_session.dropna(how="any")
                if df_session.empty:
                    continue
                    
                df_session["date"] = df_session.index
                df_session["symbol"] = sym
                
                candles = df_session.to_dict("records")
                trades = simulate_long_short_clean(candles, nifty_close, nifty_ema)
                all_trades.extend(trades)
            except Exception as e:
                pass
                
        wins = sum(1 for t in all_trades if t["result"] == "WIN")
        losses = sum(1 for t in all_trades if t["result"] == "LOSS")
        be = sum(1 for t in all_trades if t["result"] == "BREAKEVEN")
        opens = sum(1 for t in all_trades if t["result"] == "OPEN")
        total_pnl = sum(t["pnl"] for t in all_trades)
        
        longs_count = sum(1 for t in all_trades if t["side"] == "LONG")
        shorts_count = sum(1 for t in all_trades if t["side"] == "SHORT")
        
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        cumulative_pnl += total_pnl
        
        daily_summaries.append({
            "date": str(d),
            "longs": longs_count,
            "shorts": shorts_count,
            "wins": wins,
            "losses": losses,
            "be": be,
            "open": opens,
            "win_rate": win_rate,
            "pnl": total_pnl
        })
        
    print("\n" + "="*95)
    print(f"{'DATE':<12}{'LONGS':>8}{'SHORTS':>8}{'WINS':>8}{'LOSSES':>8}{'B/EVEN':>8}{'WIN RATE%':>12}{'NET PnL (INR)':>18}")
    print("-"*95)
    for s in daily_summaries:
        print(f"{s['date']:<12}{s['longs']:>8}{s['shorts']:>8}{s['wins']:>8}{s['losses']:>8}{s['be']:>8}{s['win_rate']:>12.1f}%{s['pnl']:>18,.0f} INR")
    print("="*95)
    print(f"CUMULATIVE 7-DAY LONG/SHORT NET PROFIT: {cumulative_pnl:,.0f} INR")
    print("="*95)

if __name__ == "__main__":
    main()
