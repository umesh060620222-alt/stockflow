import datetime
import yfinance as yf
import pandas as pd

# First 20 highly liquid stocks from SC.TRADING_LIST
SYMS = [
    "ICICIBANK.NS", "AXISBANK.NS", "SBIN.NS", "HCLTECH.NS", "WIPRO.NS",
    "TECHM.NS", "DRREDDY.NS", "CIPLA.NS", "SUNPHARMA.NS", "TATAMOTORS.NS",
    "HDFCBANK.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "HINDALCO.NS", "ADANIPORTS.NS",
    "JSWSTEEL.NS", "INDUSINDBK.NS", "SHRIRAMFIN.NS", "TATACONSUM.NS", "COALINDIA.NS"
]

PULLBACK_1 = 0.003  # 0.3% drop from peak
PULLBACK_2 = 0.001  # 0.1% bounce from trough
SL_PCT = 0.003      # 0.3% stop loss
TARGET_PCT = 0.003  # 0.3% target

def simulate_user_pullback(candles):
    if len(candles) == 0:
        return None, None, None, "NO DATA", None, None, None, None

    peak1 = None
    trough1 = None
    stage = 1  
    
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        
        if stage == 1:
            if peak1 is None or high > peak1:
                peak1 = high
            else:
                trough1 = low
                stage = 2
            continue
            
        if stage == 2:
            if high > peak1:
                peak1 = high
                trough1 = low
                stage = 1
                continue
            
            trough1 = min(trough1, low)
            if trough1 <= peak1 * (1 - PULLBACK_1):
                stage = 3
            continue
            
        if stage == 3:
            if low < trough1:
                trough1 = low
            
            bounce_level = trough1 * (1 + PULLBACK_2)
            if high >= bounce_level:
                entry = bounce_level
                sl = entry * (1 - SL_PCT)
                target = entry * (1 + TARGET_PCT)
                entry_time = c["date"].strftime("%H:%M")
                
                for w in candles[i+1:]:
                    w_low = float(w["low"])
                    w_high = float(w["high"])
                    if w_low <= sl:
                        return peak1, trough1, entry, "LOSS", entry_time, w["date"].strftime("%H:%M"), sl, target
                    if w_high >= target:
                        return peak1, trough1, entry, "WIN", entry_time, w["date"].strftime("%H:%M"), sl, target
                return peak1, trough1, entry, "OPEN", entry_time, "-", sl, target
            continue
            
    return peak1, trough1, None, f"NO ENTRY (stage {stage})", None, None, None, None

def main():
    print("Checking daily history to identify stocks that opened >= 0.5% higher than yesterday's close...")
    
    # Download daily history for returns calculation (pull 5d to ensure we get last 2 trading days)
    daily = yf.download(SYMS, period="5d", interval="1d", group_by="ticker", progress=False)
    
    # Also fetch today's 1-minute data for simulation and accurate open price checks
    print("Fetching today's 1m intraday data...")
    raw_1m = yf.download(SYMS, period="1d", interval="1m", group_by="ticker", progress=False)
    
    qualified_syms = []
    symbol_gaps = {}
    candles_by_sym = {}
    
    for sym in SYMS:
        try:
            # 1. Get yesterday's close
            if len(SYMS) > 1:
                df_daily = daily[sym].copy()
            else:
                df_daily = daily.copy()
            df_daily = df_daily.dropna(how="all")
            if len(df_daily) < 2:
                continue
            close_yesterday = float(df_daily["Close"].iloc[-2])
            
            # 2. Get today's first minute candle open
            if len(SYMS) > 1:
                df_1m = raw_1m[sym].copy()
            else:
                df_1m = raw_1m.copy()
            df_1m = df_1m.dropna(how="all")
            if df_1m.empty:
                continue
                
            df_1m.columns = df_1m.columns.str.lower()
            df_1m = df_1m[["open", "high", "low", "close", "volume"]]
            df_1m = df_1m.dropna(how="any")
            if df_1m.empty:
                continue
                
            if df_1m.index.tz is None:
                df_1m.index = df_1m.index.tz_localize("UTC")
            df_1m.index = df_1m.index.tz_convert("Asia/Kolkata")
            df_1m["date"] = df_1m.index
            
            first_candle_open = float(df_1m["open"].iloc[0])
            gap = (first_candle_open - close_yesterday) / close_yesterday * 100
            
            symbol_gaps[sym] = gap
            candles_by_sym[sym] = df_1m.to_dict("records")
            
            if gap >= 0.5:
                qualified_syms.append(sym)
        except Exception as e:
            # print(f"Error checking gap for {sym}: {e}")
            pass

    print("\nAll Checked Symbol Open Gaps (Sorted):")
    sorted_gaps = sorted(symbol_gaps.items(), key=lambda x: x[1], reverse=True)
    for sym, gap in sorted_gaps:
        print(f"  - {sym.replace('.NS', '')}: {gap:+.2f}%")

    if not qualified_syms:
        print("\nNo symbols opened >= 0.5% higher today.")
        return
        
    rows = []
    wins = losses = opens = no_entries = 0
    
    for sym in qualified_syms:
        try:
            candles = candles_by_sym[sym]
            peak1, trough1, entry, result, entry_time, exit_time, sl, target = simulate_user_pullback(candles)
            
            exit_price = target if result == "WIN" else (sl if result == "LOSS" else None)
            rows.append((sym, peak1, trough1, entry, exit_price, result, entry_time, exit_time))
            
            if result == "WIN":
                wins += 1
            elif result == "LOSS":
                losses += 1
            elif result == "OPEN":
                opens += 1
            else:
                no_entries += 1
                
        except Exception as e:
            print(f"Error processing {sym}: {e}")
            rows.append((sym, None, None, None, None, f"ERROR: {e}", None, None))
            no_entries += 1

    print("\n" + "="*90)
    print(f"{'SYMBOL':<15}{'OPEN GAP%':>12}{'ENTRY':>10}{'EXIT':>10}  {'RESULT':<10}{'ENTRY@':>8}{'EXIT@':>8}")
    print("-"*90)
    for r in rows:
        sym, peak1, trough1, entry, exit_price, result, entry_time, exit_time = r
        sym_clean = sym.replace(".NS", "")
        gap_str = f"{symbol_gaps[sym]:+.2f}%"
        if entry is None:
            print(f"{sym_clean:<15}{gap_str:>12}{'-':>10}{'-':>10}  {result:<10}{'-':>8}{'-':>8}")
        else:
            exit_str = f"{exit_price:.2f}" if exit_price else "-"
            print(f"{sym_clean:<15}{gap_str:>12}{entry:>10.2f}{exit_str:>10}  {result:<10}{entry_time:>8}{exit_time:>8}")
            
    print("="*90)
    print(f"SUMMARY: Wins: {wins} | Losses: {losses} | Open: {opens} | No Entry: {no_entries}")
    print("="*90)

if __name__ == "__main__":
    main()
