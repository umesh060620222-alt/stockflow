import datetime
import yfinance as yf
import pandas as pd
import math

# Strategy settings for Nifty 50 Spot Index
PULLBACK_1 = 2.5      # 2.5x ATR
PULLBACK_2 = 0.7      # 0.7x ATR
SL_ATR_MULT = 1.0     # Stop loss: 1.0x ATR
TARGET_ATR_MULT = 2.0 # Target: 2.0x ATR
VOL_MULT = 1.5        # Volume surge multiplier
FRICTION = 0.0016     # 0.16% total friction (brokerage + slippage)

MIN_SL_PCT = 0.0015    # Minimum Stop Loss of 0.15% (approx 36 points on Nifty)
MIN_TARGET_PCT = 0.0030 # Minimum Target of 0.30% (approx 72 points on Nifty)

# Nifty Option Contract Specifications
LOT_SIZE = 75         # Nifty Lot size (adjust if needed, e.g. 75 or 50)
ATM_DELTA = 0.50      # At-The-Money option delta
EST_ATM_PREMIUM = 150.0  # Estimated entry premium in points (typical weekly ATM)
LOTS_CAPITAL = 100000.0  # Allocation capital of 1 Lakh per trade

# Transaction Costs (brokerage + slippage)
OPTIONS_BROKERAGE_INR = 40.0
OPTIONS_SLIPPAGE_POINTS = 2.0

def simulate_nifty_options_improved(candles, nifty_open):
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

    closes = pd.Series([float(c["close"]) for c in candles])
    ema_series = closes.ewm(span=15, adjust=False).mean().tolist()
    for idx, c in enumerate(candles):
        c["nifty_ema"] = ema_series[idx]

    # Long setup variables
    l_peak = None
    l_trough = None
    l_peak_atr = None
    l_stage = 1
    
    # Short setup variables
    s_trough = None
    s_peak = None
    s_trough_atr = None
    s_stage = 1
    
    trades = []
    locked_until_idx = -1
    
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        close = float(c["close"])
        atr = float(c["atr"])
        ts = c["date"]
        nifty_ema = float(c["nifty_ema"])
        
        if i <= locked_until_idx:
            continue
            
        # Nifty 15-EMA filter
        is_nifty_above_ema = close > nifty_ema
        is_nifty_below_ema = close < nifty_ema
        
        # Nifty Daily Open check
        is_nifty_green_today = close > nifty_open
        is_nifty_red_today = close < nifty_open
        
        time_str = ts.strftime("%H:%M")
        is_valid_time = ("10:00" <= time_str < "11:00") or ("14:00" <= time_str < "15:30")
        
        # ------------------------------------
        # CALL OPTION ENTRY (LONG SETUP)
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
                # Combined Trend Filter: Nifty above Daily Open AND Nifty above 15-EMA
                if is_valid_time and is_nifty_above_ema and is_nifty_green_today:
                    entry = bounce_level
                    raw_sl_pct = (SL_ATR_MULT * atr) / entry
                    raw_target_pct = (TARGET_ATR_MULT * atr) / entry
                    actual_sl_pct = max(raw_sl_pct, MIN_SL_PCT)
                    actual_target_pct = max(raw_target_pct, MIN_TARGET_PCT)
                    
                    sl = entry * (1 - actual_sl_pct)
                    target = entry * (1 + actual_target_pct)
                    
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
                            
                            halfway_level = entry + 0.5 * (target - entry)
                            if w_high >= halfway_level:
                                reached_halfway = True
                                current_sl = entry
                                
                            if w_low <= current_sl:
                                trade_result = "LOSS" if not reached_halfway else "BREAKEVEN"
                                exit_price_val = current_sl
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
                                
                    lot_cost = EST_ATM_PREMIUM * LOT_SIZE
                    lots = math.floor(LOTS_CAPITAL / lot_cost)
                    total_shares = lots * LOT_SIZE
                    
                    if trade_result == "WIN":
                        spot_change = target - entry
                        premium_change = spot_change * ATM_DELTA
                        pnl_gross = premium_change * total_shares
                        pnl_net = pnl_gross - (lots * OPTIONS_BROKERAGE_INR) - (OPTIONS_SLIPPAGE_POINTS * total_shares)
                    elif trade_result == "LOSS":
                        spot_change = sl - entry
                        premium_change = spot_change * ATM_DELTA
                        pnl_gross = premium_change * total_shares
                        pnl_net = pnl_gross - (lots * OPTIONS_BROKERAGE_INR) - (OPTIONS_SLIPPAGE_POINTS * total_shares)
                    elif trade_result == "BREAKEVEN":
                        pnl_net = - (lots * OPTIONS_BROKERAGE_INR) - (OPTIONS_SLIPPAGE_POINTS * total_shares)
                    else:
                        pnl_net = 0.0
                        
                    trades.append({
                        "side": "BUY CALL (CE)",
                        "entry_time": time_str,
                        "exit_time": exit_time,
                        "duration": duration,
                        "entry": entry,
                        "exit": exit_price_val,
                        "result": trade_result,
                        "pnl": pnl_net,
                        "lots": lots
                    })
                    long_triggered = True
                    l_peak = None
                    l_trough = None
                    l_peak_atr = None
                    l_stage = 1

        # ------------------------------------
        # PUT OPTION ENTRY (SHORT SETUP)
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
                    # Combined Trend Filter: Nifty below Daily Open AND Nifty below 15-EMA
                    if is_valid_time and is_nifty_below_ema and is_nifty_red_today:
                        entry = short_trigger_level
                        raw_sl_pct = (SL_ATR_MULT * atr) / entry
                        raw_target_pct = (TARGET_ATR_MULT * atr) / entry
                        actual_sl_pct = max(raw_sl_pct, MIN_SL_PCT)
                        actual_target_pct = max(raw_target_pct, MIN_TARGET_PCT)
                        
                        sl = entry * (1 + actual_sl_pct)
                        target = entry * (1 - actual_target_pct)
                        
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
                                
                                halfway_level = entry - 0.5 * (entry - target)
                                if w_low <= halfway_level:
                                    reached_halfway = True
                                    current_sl = entry
                                    
                                if w_high >= current_sl:
                                    trade_result = "LOSS" if not reached_halfway else "BREAKEVEN"
                                    exit_price_val = current_sl
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
                                    
                        lot_cost = EST_ATM_PREMIUM * LOT_SIZE
                        lots = math.floor(LOTS_CAPITAL / lot_cost)
                        total_shares = lots * LOT_SIZE
                        
                        if trade_result == "WIN":
                            spot_change = entry - target
                            premium_change = spot_change * ATM_DELTA
                            pnl_gross = premium_change * total_shares
                            pnl_net = pnl_gross - (lots * OPTIONS_BROKERAGE_INR) - (OPTIONS_SLIPPAGE_POINTS * total_shares)
                        elif trade_result == "LOSS":
                            spot_change = entry - sl
                            premium_change = spot_change * ATM_DELTA
                            pnl_gross = premium_change * total_shares
                            pnl_net = pnl_gross - (lots * OPTIONS_BROKERAGE_INR) - (OPTIONS_SLIPPAGE_POINTS * total_shares)
                        elif trade_result == "BREAKEVEN":
                            pnl_net = - (lots * OPTIONS_BROKERAGE_INR) - (OPTIONS_SLIPPAGE_POINTS * total_shares)
                        else:
                            pnl_net = 0.0
                            
                        trades.append({
                            "side": "BUY PUT (PE)",
                            "entry_time": time_str,
                            "exit_time": exit_time,
                            "duration": duration,
                            "entry": entry,
                            "exit": exit_price_val,
                            "result": trade_result,
                            "pnl": pnl_net,
                            "lots": lots
                        })
                        s_trough = None
                        s_peak = None
                        s_trough_atr = None
                        s_stage = 1

    return trades

def main():
    print("Downloading 7 calendar days of Nifty 50 spot data (^NSEI)...")
    raw = yf.download("^NSEI", period="7d", interval="1m", progress=False)
    
    if raw.empty:
        print("Error: Could not retrieve Nifty 50 data.")
        return
        
    df = raw.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Kolkata")
    
    dates = sorted(list(set(df.index.date)))
    
    daily_summaries = []
    cumulative_pnl = 0.0
    
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
        nifty_open = float(df_session["open"].iloc[0])
        
        candles = df_session.to_dict("records")
        
        trades = simulate_nifty_options_improved(candles, nifty_open)
        
        wins = sum(1 for t in trades if t["result"] == "WIN")
        losses = sum(1 for t in trades if t["result"] == "LOSS")
        be = sum(1 for t in trades if t["result"] == "BREAKEVEN")
        opens = sum(1 for t in trades if t["result"] == "OPEN")
        total_pnl = sum(t["pnl"] for t in trades)
        
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
        cumulative_pnl += total_pnl
        
        daily_summaries.append({
            "date": str(d),
            "trades_count": len(trades),
            "wins": wins,
            "losses": losses,
            "be": be,
            "open": opens,
            "win_rate": win_rate,
            "pnl": total_pnl
        })
        
    print("\n" + "="*95)
    print(f"SESSION REPORT (WITH COMBINED DAILY OPEN + 15-EMA TREND GATES)")
    print("="*95)
    print(f"{'DATE':<12}{'TRADES':>8}{'WINS':>8}{'LOSSES':>8}{'B/EVEN':>8}{'WIN RATE%':>12}{'NET PnL (INR)':>18}")
    print("-"*95)
    for s in daily_summaries:
        print(f"{s['date']:<12}{s['trades_count']:>8}{s['wins']:>8}{s['losses']:>8}{s['be']:>8}{s['win_rate']:>12.1f}%{s['pnl']:>18,.0f} INR")
    print("="*95)
    print(f"CUMULATIVE 7-DAY NIFTY OPTIONS NET PROFIT: {cumulative_pnl:,.0f} INR")
    print("="*95)

if __name__ == "__main__":
    main()
