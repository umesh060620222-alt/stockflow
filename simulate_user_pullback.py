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
    # stage 1: finding peak1 (initial rise)
    # stage 2: waiting for 0.3% drop from peak1, tracking trough1
    # stage 3: waiting for 0.1% bounce off trough1 to enter long
    
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        
        if stage == 1:
            if peak1 is None or high > peak1:
                peak1 = high
            else:
                # price started to drop
                trough1 = low
                stage = 2
            continue
            
        if stage == 2:
            if high > peak1:
                # price broke to new high instead of dropping 0.3%, reset peak
                peak1 = high
                trough1 = low
                stage = 1
                continue
            
            trough1 = min(trough1, low)
            # check if 0.3% drop is achieved
            if trough1 <= peak1 * (1 - PULLBACK_1):
                stage = 3
            continue
            
        if stage == 3:
            # If price breaks below trough1, update it
            if low < trough1:
                trough1 = low
            
            # check for 0.1% bounce from trough1
            bounce_level = trough1 * (1 + PULLBACK_2)
            if high >= bounce_level:
                entry = bounce_level
                sl = entry * (1 - SL_PCT)
                target = entry * (1 + TARGET_PCT)
                entry_time = c["date"].strftime("%H:%M")
                
                # evaluate outcome from subsequent candles
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
    print(f"Downloading 1m data for {len(SYMS)} symbols for today...")
    # Fetch today's data
    raw = yf.download(SYMS, period="1d", interval="1m", group_by="ticker", progress=False)
    
    if raw.empty:
        print("Error: No data retrieved from yfinance.")
        return
        
    rows = []
    wins = losses = opens = no_entries = 0
    
    for sym in SYMS:
        try:
            if len(SYMS) > 1:
                df = raw[sym].copy()
            else:
                df = raw.copy()
            
            df = df.dropna(how="all")
            if df.empty:
                rows.append((sym, None, None, None, None, "NO DATA", None, None))
                no_entries += 1
                continue
                
            df.columns = df.columns.str.lower()
            df = df[["open", "high", "low", "close", "volume"]]
            df = df.dropna(how="any")
            
            # Align time zone and format index
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("Asia/Kolkata")
            df["date"] = df.index
            
            candles = df.to_dict("records")
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
    print(f"{'SYMBOL':<15}{'ENTRY':>10}{'EXIT':>10}  {'RESULT':<10}{'ENTRY@':>8}{'EXIT@':>8}")
    print("-"*90)
    for r in rows:
        sym, peak1, trough1, entry, exit_price, result, entry_time, exit_time = r
        sym_clean = sym.replace(".NS", "")
        if entry is None:
            print(f"{sym_clean:<15}{'-':>10}{'-':>10}  {result:<10}{'-':>8}{'-':>8}")
        else:
            exit_str = f"{exit_price:.2f}" if exit_price else "-"
            print(f"{sym_clean:<15}{entry:>10.2f}{exit_str:>10}  {result:<10}{entry_time:>8}{exit_time:>8}")
            
    print("="*80)
    print(f"SUMMARY: Wins: {wins} | Losses: {losses} | Open: {opens} | No Entry: {no_entries}")
    print("="*80)

if __name__ == "__main__":
    main()
