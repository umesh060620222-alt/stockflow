import datetime
import yfinance as yf
import pandas as pd
from collections import defaultdict

# First 20 highly liquid stocks from SC.TRADING_LIST
SYMS = [
    "ICICIBANK.NS", "AXISBANK.NS", "SBIN.NS", "HCLTECH.NS", "WIPRO.NS",
    "TECHM.NS", "DRREDDY.NS", "CIPLA.NS", "SUNPHARMA.NS", "TATAMOTORS.NS",
    "HDFCBANK.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "HINDALCO.NS", "ADANIPORTS.NS",
    "JSWSTEEL.NS", "INDUSINDBK.NS", "SHRIRAMFIN.NS", "TATACONSUM.NS", "COALINDIA.NS"
]

PULLBACK_1 = 2.5    # 2.5x ATR
PULLBACK_2 = 0.7    # 0.7x ATR
SL_PCT = 0.004      # 0.4% stop loss
TARGET_PCT = 0.008  # 0.8% target
VOL_MULT = 1.5      # Volume surge multiplier
FRICTION = 0.0016   # 0.16% total friction (brokerage + slippage)

def simulate_user_pullback(candles, nifty_series, nifty_open, use_nifty_filter):
    if len(candles) == 0:
        return None, None, None, "NO DATA", None, None, None, None

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

    peak1 = None
    trough1 = None
    peak_atr = None
    stage = 1  
    
    vol_history = []
    
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        vol = float(c["volume"])
        atr = float(c["atr"])
        ts = c["date"]
        
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
                # 1. Time filter
                time_str = ts.strftime("%H:%M")
                is_valid_time = time_str < "11:00" or time_str >= "14:00"
                
                # 2. Volume filter
                if len(vol_history) >= 3:
                    vol_avg = sum(vol_history) / len(vol_history)
                else:
                    vol_avg = 0
                is_valid_vol = (vol > VOL_MULT * vol_avg) if vol_avg > 0 else True
                
                # 3. Nifty filter
                is_nifty_green = True
                if use_nifty_filter and nifty_series is not None and nifty_open is not None:
                    nifty_price = nifty_series.get(ts)
                    if nifty_price is None:
                        closest_ts = min(nifty_series.index, key=lambda x: abs((x - ts).total_seconds()))
                        nifty_price = nifty_series.loc[closest_ts]
                    is_nifty_green = nifty_price > nifty_open
                
                if is_valid_time and is_valid_vol and is_nifty_green:
                    entry = bounce_level
                    sl = entry * (1 - SL_PCT)
                    target = entry * (1 + TARGET_PCT)
                    entry_time = time_str
                    
                    for w in candles[i+1:]:
                        w_low = float(w["low"])
                        w_high = float(w["high"])
                        if w_low <= sl:
                            return peak1, trough1, entry, "LOSS", entry_time, w["date"].strftime("%H:%M"), sl, target
                        if w_high >= target:
                            return peak1, trough1, entry, "WIN", entry_time, w["date"].strftime("%H:%M"), sl, target
                    return peak1, trough1, entry, "OPEN", entry_time, "-", sl, target
            
            vol_history.append(vol)
            if len(vol_history) > 12:
                vol_history.pop(0)
            continue
            
    return peak1, trough1, None, f"NO ENTRY (stage {stage})", None, None, None, None

def main():
    print("Downloading historical 1m data for the last 5 days to find previous trading session...")
    raw = yf.download(SYMS + ["^NSEI"], period="5d", interval="1m", group_by="ticker", progress=False)
    
    if raw.empty:
        print("Error: No data retrieved.")
        return
        
    # Get all unique dates in the index
    nifty_raw = raw["^NSEI"].dropna(how="all")
    if nifty_raw.empty:
        print("Error: Nifty 50 data missing.")
        return
        
    if nifty_raw.index.tz is None:
        nifty_raw.index = nifty_raw.index.tz_localize("UTC")
    nifty_raw.index = nifty_raw.index.tz_convert("Asia/Kolkata")
    
    dates = sorted(list(set(nifty_raw.index.date)))
    print(f"Available trading dates: {[str(d) for d in dates]}")
    
    if len(dates) < 2:
        print("Error: Need at least 2 trading dates to find the previous session.")
        return
        
    # The last date is today, so the previous session is dates[-2]
    prev_date = dates[-2]
    print(f"\nTargeting Previous Trading Session: {prev_date}")
    
    # Process Nifty 50 for the previous session
    prev_nifty = nifty_raw[nifty_raw.index.date == prev_date]
    nifty_open = float(prev_nifty["Open"].iloc[0])
    nifty_series = prev_nifty["Close"].astype(float)
    
    print(f"Nifty 50 Open on {prev_date}: {nifty_open:.2f} | Close: {nifty_series.iloc[-1]:.2f}")
    
    rows = []
    wins = losses = opens = no_entries = 0
    
    for sym in SYMS:
        if sym == "^NSEI":
            continue
        try:
            if len(SYMS) > 1:
                df = raw[sym].copy()
            else:
                df = raw.copy()
                
            df = df.dropna(how="all")
            if df.empty:
                continue
                
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("Asia/Kolkata")
            
            # Filter for previous session
            df_prev = df[df.index.date == prev_date]
            if df_prev.empty:
                continue
                
            df_prev.columns = df_prev.columns.str.lower()
            df_prev = df_prev[["open", "high", "low", "close", "volume"]]
            df_prev = df_prev.dropna(how="any")
            if df_prev.empty:
                continue
                
            df_prev["date"] = df_prev.index
            
            candles = df_prev.to_dict("records")
            peak1, trough1, entry, result, entry_time, exit_time, sl, target = simulate_user_pullback(
                candles, nifty_series, nifty_open, use_nifty_filter=True
            )
            
            exit_price = target if result == "WIN" else (sl if result == "LOSS" else None)
            
            if entry is not None:
                rows.append((sym, entry, exit_price, result, entry_time, exit_time))
                if result == "WIN":
                    wins += 1
                elif result == "LOSS":
                    losses += 1
                elif result == "OPEN":
                    opens += 1
            else:
                no_entries += 1
                
        except Exception as e:
            # print(f"Error processing {sym}: {e}")
            pass
            
    print("\n" + "="*95)
    print(f"{'SYMBOL':<15}{'ENTRY PRICE':>12}{'EXIT PRICE':>12}  {'RESULT':<10}{'ENTRY@':>8}{'EXIT@':>8}{'NET PnL (INR)':>12}")
    print("-"*95)
    
    total_pnl = 0.0
    for r in rows:
        sym, entry, exit_price, result, entry_time, exit_time = r
        sym_clean = sym.replace(".NS", "")
        
        # Calculate PnL for sequential 1 Lakh trade
        # Win: +0.8% - 0.16% = +0.64% net. Loss: -0.4% - 0.16% = -0.56% net
        if result == "WIN":
            pnl = 100000 * (TARGET_PCT - FRICTION)
        elif result == "LOSS":
            pnl = 100000 * (-SL_PCT - FRICTION)
        else:
            pnl = 0.0
            
        total_pnl += pnl
        exit_str = f"{exit_price:.2f}" if exit_price else "-"
        pnl_str = f"{pnl:+.0f}" if result in ("WIN", "LOSS") else "-"
        print(f"{sym_clean:<15}{entry:>12.2f}{exit_str:>12}  {result:<10}{entry_time:>8}{exit_time:>8}{pnl_str:>12}")
        
    print("="*95)
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0.0
    print(f"SUMMARY FOR {prev_date}: Wins: {wins} | Losses: {losses} | Open: {opens} | Win Rate: {win_rate:.1f}%")
    print(f"TOTAL NET PROFIT (assuming 100,000 INR per trade sequentially): {total_pnl:,.0f} INR")
    print("="*95)

if __name__ == "__main__":
    main()
