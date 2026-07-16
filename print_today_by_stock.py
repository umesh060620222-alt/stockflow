import datetime
import yfinance as yf
import pandas as pd
from collections import defaultdict

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

def simulate_atr_exits_with_breakeven(candles, nifty_close, nifty_ema):
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

    peak1 = None
    trough1 = None
    peak_atr = None
    stage = 1  
    
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
            
        if stage == 1:
            if peak1 is None or high > peak1:
                peak1 = high
                peak_atr = atr
            else:
                trough1 = low
                stage = 2
            vol_history.append(vol)
            if len(vol_history) > 12:
                vol_history.pop(0)
            continue
            
        if stage == 2:
            if high > peak1:
                peak1 = high
                peak_atr = atr
                trough1 = low
                stage = 1
                vol_history.append(vol)
                if len(vol_history) > 12:
                    vol_history.pop(0)
                continue
            
            trough1 = min(trough1, low)
            drop_required = PULLBACK_1 * (peak_atr if peak_atr else atr)
            if trough1 <= peak1 - drop_required:
                stage = 3
            vol_history.append(vol)
            if len(vol_history) > 12:
                vol_history.pop(0)
            continue
            
        if stage == 3:
            if low < trough1:
                trough1 = low
            
            bounce_required = PULLBACK_2 * atr
            bounce_level = trough1 + bounce_required
            if high >= bounce_level:
                time_str = ts.strftime("%H:%M")
                is_valid_time = ("10:00" <= time_str < "11:00") or ("14:00" <= time_str < "15:30")
                
                if len(vol_history) >= 3:
                    vol_avg = sum(vol_history) / len(vol_history)
                else:
                    vol_avg = 0
                is_valid_vol = (vol > VOL_MULT * vol_avg) if vol_avg > 0 else True
                
                is_nifty_bullish = True
                if nifty_close is not None and nifty_ema is not None:
                    nifty_p = nifty_close.get(ts)
                    nifty_e = nifty_ema.get(ts)
                    if nifty_p is None and not nifty_close.empty:
                        closest_ts = min(nifty_close.index, key=lambda x: abs((x - ts).total_seconds()))
                        nifty_p = nifty_close.loc[closest_ts]
                        nifty_e = nifty_ema.loc[closest_ts]
                    if nifty_p is not None and nifty_e is not None:
                        is_nifty_bullish = nifty_p > nifty_e
                
                if is_valid_time and is_valid_vol and is_nifty_bullish:
                    entry = bounce_level
                    
                    raw_sl_pct = (SL_ATR_MULT * atr) / entry
                    raw_target_pct = (TARGET_ATR_MULT * atr) / entry
                    
                    actual_sl_pct = max(raw_sl_pct, MIN_SL_PCT)
                    actual_target_pct = max(raw_target_pct, MIN_TARGET_PCT)
                    
                    sl = entry * (1 - actual_sl_pct)
                    target = entry * (1 + actual_target_pct)
                    
                    trade_result = "OPEN"
                    exit_price_val = None
                    
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
                            
                    if trade_result == "WIN":
                        pnl_pct = actual_target_pct - FRICTION
                    elif trade_result == "LOSS":
                        pnl_pct = -actual_sl_pct - FRICTION
                    elif trade_result == "BREAKEVEN":
                        pnl_pct = 0.0 - FRICTION
                    else:
                        pnl_pct = 0.0
                        
                    pnl_inr = 100000 * pnl_pct if trade_result in ("WIN", "LOSS", "BREAKEVEN") else 0.0
                    
                    trades.append({
                        "sym": c["symbol"],
                        "result": trade_result,
                        "pnl": pnl_inr
                    })
                    
                    peak1 = None
                    trough1 = None
                    peak_atr = None
                    stage = 1
                    
            vol_history.append(vol)
            if len(vol_history) > 12:
                vol_history.pop(0)
            continue
            
    return trades

def main():
    raw = yf.download(SYMS + ["^NSEI"], period="5d", interval="1m", group_by="ticker", progress=False)
    
    ticker = "ICICIBANK.NS"
    df_sample = raw[ticker].dropna(how="all")
    if df_sample.index.tz is None:
        df_sample.index = df_sample.index.tz_localize("UTC")
    df_sample.index = df_sample.index.tz_convert("Asia/Kolkata")
    
    dates = sorted(list(set(df_sample.index.date)))
    today_date = dates[-1] # July 13
    
    # Pre-process Nifty EMA for today
    nifty_df = raw["^NSEI"].dropna(how="all").copy()
    if nifty_df.index.tz is None:
        nifty_df.index = nifty_df.index.tz_localize("UTC")
    nifty_df.index = nifty_df.index.tz_convert("Asia/Kolkata")
    
    nifty_session = nifty_df[nifty_df.index.date == today_date]
    nifty_close = nifty_session["Close"].astype(float)
    nifty_ema = nifty_close.ewm(span=15, adjust=False).mean()
    
    # Track stats by stock
    stock_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "be": 0, "pnl": 0.0})
    
    for sym in SYMS:
        try:
            df = raw[sym].copy()
            df = df.dropna(how="all")
            if df.empty:
                continue
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("Asia/Kolkata")
            
            df_session = df[df.index.date == today_date]
            df_session.columns = df_session.columns.str.lower()
            df_session = df_session[["open", "high", "low", "close", "volume"]]
            df_session = df_session.dropna(how="any")
            if df_session.empty:
                continue
                
            df_session["date"] = df_session.index
            df_session["symbol"] = sym
            
            candles = df_session.to_dict("records")
            trades = simulate_atr_exits_with_breakeven(candles, nifty_close, nifty_ema)
            
            for t in trades:
                sym_clean = sym.replace(".NS", "")
                res = t["result"]
                stock_stats[sym_clean]["pnl"] += t["pnl"]
                if res == "WIN":
                    stock_stats[sym_clean]["wins"] += 1
                elif res == "LOSS":
                    stock_stats[sym_clean]["losses"] += 1
                elif res == "BREAKEVEN":
                    stock_stats[sym_clean]["be"] += 1
        except Exception as e:
            pass
            
    print("\n" + "="*80)
    print(f"{'STOCK':<15}{'WINS':>8}{'LOSSES':>10}{'B/EVEN':>10}{'NET PnL (INR)':>18}{'PERFORMANCE':>18}")
    print("-"*80)
    
    total_pnl = 0.0
    # Sort by Net PnL descending
    sorted_stats = sorted(stock_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
    
    for sym, s in sorted_stats:
        total_pnl += s["pnl"]
        perf_indicator = "GREEN" if s["pnl"] > 0 else ("RED" if s["pnl"] < 0 else "FLAT")
        print(f"{sym:<15}{s['wins']:>8}{s['losses']:>10}{s['be']:>10}{s['pnl']:>18,.0f} INR{perf_indicator:>14}")
        
    print("="*80)
    print(f"TOTAL NET PROFIT: {total_pnl:,.0f} INR")
    print("="*80)

if __name__ == "__main__":
    main()
