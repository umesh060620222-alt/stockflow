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

PULLBACK_1 = 0.010  # Increased to 1.0%
PULLBACK_2 = 0.001  # 0.1% bounce from trough
SL_PCT = 0.003      # 0.3% stop loss
TARGET_PCT = 0.003  # 0.3% target
VOL_MULT = 1.5      # Volume surge multiplier

def simulate_user_pullback(candles, nifty_series, nifty_open, use_nifty_filter):
    if len(candles) == 0:
        return None, None, None, "NO DATA", None, None, None, None

    peak1 = None
    trough1 = None
    stage = 1  
    
    vol_history = []
    
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        vol = float(c["volume"])
        ts = c["date"]
        
        if stage == 1:
            if peak1 is None or high > peak1:
                peak1 = high
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
                trough1 = low
                stage = 1
                vol_history.append(vol)
                if len(vol_history) > 12:
                    vol_history.pop(0)
                continue
            
            trough1 = min(trough1, low)
            if trough1 <= peak1 * (1 - PULLBACK_1):
                stage = 3
                
            vol_history.append(vol)
            if len(vol_history) > 12:
                vol_history.pop(0)
            continue
            
        if stage == 3:
            if low < trough1:
                trough1 = low
            
            bounce_level = trough1 * (1 + PULLBACK_2)
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
                    # Find closest nifty price
                    nifty_price = nifty_series.get(ts)
                    if nifty_price is None:
                        # Fallback to nearest index if timestamp doesn't match perfectly
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

def run_simulation(raw, qualified_syms, nifty_series, nifty_open, use_nifty_filter):
    rows = []
    wins = losses = opens = no_entries = 0
    
    for sym in qualified_syms:
        try:
            if len(raw.columns.levels[0]) > 1:
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
            
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("Asia/Kolkata")
            df["date"] = df.index
            
            candles = df.to_dict("records")
            peak1, trough1, entry, result, entry_time, exit_time, sl, target = simulate_user_pullback(
                candles, nifty_series, nifty_open, use_nifty_filter
            )
            
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
            rows.append((sym, None, None, None, None, f"ERROR: {e}", None, None))
            no_entries += 1
            
    return rows, wins, losses, opens, no_entries

def main():
    print(f"Downloading 1m data for 20 symbols + Nifty 50 (^NSEI) for today...")
    raw = yf.download(SYMS + ["^NSEI"], period="1d", interval="1m", group_by="ticker", progress=False)
    
    if raw.empty:
        print("Error: No data retrieved from yfinance.")
        return
    
    # Process Nifty 50
    nifty_series = None
    nifty_open = None
    try:
        nifty_df = raw["^NSEI"].dropna(how="all")
        if not nifty_df.empty:
            if nifty_df.index.tz is None:
                nifty_df.index = nifty_df.index.tz_localize("UTC")
            nifty_df.index = nifty_df.index.tz_convert("Asia/Kolkata")
            
            nifty_open = float(nifty_df["Open"].iloc[0])
            nifty_series = nifty_df["Close"].astype(float)
            print(f"Nifty 50 Daily Open: {nifty_open:.2f} | Last Close: {nifty_series.iloc[-1]:.2f} (Status: {'GREEN' if nifty_series.iloc[-1] > nifty_open else 'RED'})")
    except Exception as e:
        print(f"Failed to process Nifty 50 index data: {e}")

    qualified_syms = [s for s in SYMS if s in raw.columns.levels[0] and s != "^NSEI"]
    
    # ----------------------------------------------------
    # RUN 1: Deeper Pullback (0.5%) without Nifty Filter
    # ----------------------------------------------------
    print("\n" + "="*80)
    print("RUN 1: Deeper Pullback (0.5%) + Time + Vol (No Nifty Filter)")
    print("="*80)
    rows_r1, wins_r1, losses_r1, opens_r1, no_entries_r1 = run_simulation(
        raw, qualified_syms, nifty_series, nifty_open, use_nifty_filter=False
    )
    
    print(f"{'SYMBOL':<15}{'ENTRY':>10}{'EXIT':>10}  {'RESULT':<10}{'ENTRY@':>8}{'EXIT@':>8}")
    print("-"*80)
    for r in rows_r1:
        sym, peak1, trough1, entry, exit_price, result, entry_time, exit_time = r
        sym_clean = sym.replace(".NS", "")
        if entry is None:
            print(f"{sym_clean:<15}{'-':>10}{'-':>10}  {result:<10}{'-':>8}{'-':>8}")
        else:
            exit_str = f"{exit_price:.2f}" if exit_price else "-"
            print(f"{sym_clean:<15}{entry:>10.2f}{exit_str:>10}  {result:<10}{entry_time:>8}{exit_time:>8}")
    
    winrate_r1 = (wins_r1 / (wins_r1 + losses_r1) * 100) if (wins_r1 + losses_r1) > 0 else 0.0
    print("-"*80)
    print(f"RUN 1 SUMMARY: Wins: {wins_r1} | Losses: {losses_r1} | Open: {opens_r1} | Win Rate: {winrate_r1:.1f}%")
    print("="*80)

    # ----------------------------------------------------
    # RUN 2: Deeper Pullback (0.5%) + Nifty Filter
    # ----------------------------------------------------
    print("\n" + "="*80)
    print("RUN 2: Deeper Pullback (0.5%) + Time + Vol + Nifty Positive Filter")
    print("="*80)
    rows_r2, wins_r2, losses_r2, opens_r2, no_entries_r2 = run_simulation(
        raw, qualified_syms, nifty_series, nifty_open, use_nifty_filter=True
    )
    
    print(f"{'SYMBOL':<15}{'ENTRY':>10}{'EXIT':>10}  {'RESULT':<10}{'ENTRY@':>8}{'EXIT@':>8}")
    print("-"*80)
    for r in rows_r2:
        sym, peak1, trough1, entry, exit_price, result, entry_time, exit_time = r
        sym_clean = sym.replace(".NS", "")
        if entry is None:
            print(f"{sym_clean:<15}{'-':>10}{'-':>10}  {result:<10}{'-':>8}{'-':>8}")
        else:
            exit_str = f"{exit_price:.2f}" if exit_price else "-"
            print(f"{sym_clean:<15}{entry:>10.2f}{exit_str:>10}  {result:<10}{entry_time:>8}{exit_time:>8}")
            
    winrate_r2 = (wins_r2 / (wins_r2 + losses_r2) * 100) if (wins_r2 + losses_r2) > 0 else 0.0
    print("-"*80)
    print(f"RUN 2 SUMMARY: Wins: {wins_r2} | Losses: {losses_r2} | Open: {opens_r2} | Win Rate: {winrate_r2:.1f}%")
    print("="*80)

if __name__ == "__main__":
    main()
