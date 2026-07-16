"""Pick stocks with buying momentum (chgPct vs prevClose >= 0.1%, any time of
day). From 9:15, track the peak; the pullback only counts once it crosses a
0.3% floor below that peak (filters out pure noise) — but once past the
floor, keep letting it fall as far as it naturally goes (uncapped) until price
stops making new lows. Track the recovery peak the same way (uncapped, ends on
the first non-extending candle), then wait for a fixed 0.1% pullback off that
recovery peak as the entry trigger. Target +0.3%, SL -0.3%."""
import datetime
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

CHG_FILTER = 0.1      # % vs prevClose — matches the live app's own "up" threshold in tdSignal
PULLBACK_1_FLOOR = 0.003  # first pullback must cross this floor before it counts as real
PULLBACK_2 = 0.001    # second pullback: 0.1% off the recovery peak — entry trigger
SL_PCT = 0.003
TARGET_PCT = 0.003

kc = Z.kite()
today = datetime.date.today()

def simulate(candles):
    peak1 = None
    peak2 = None
    trough1 = None
    stage = 1  # 1 = tracking peak1, waiting to cross the 0.3% floor; 2 = past the floor, tracking the true (uncapped) trough; 3 = tracking the recovery peak (uncapped), watching for the 0.1% entry trigger
    for i, c in enumerate(candles):
        if stage == 1:
            if peak1 is None or c["high"] > peak1:
                peak1 = c["high"] if peak1 is None else max(peak1, c["high"])
                continue
            floor_level = peak1 * (1 - PULLBACK_1_FLOOR)
            if c["low"] > floor_level:
                continue  # dip so far hasn't crossed the 0.3% floor yet
            # floor crossed — real pullback confirmed, now track how deep it truly goes
            trough1 = c["low"]
            stage = 2
            continue
        if stage == 2:
            if c["low"] < trough1:
                trough1 = c["low"]
                continue
            # price stopped making new lows — trough found, track the recovery peak
            peak2 = c["high"]
            stage = 3
            continue
        if stage == 3:
            if c["high"] > peak2:
                peak2 = c["high"]
                continue
            pullback_level = peak2 * (1 - PULLBACK_2)
            if c["low"] > pullback_level:
                continue
            entry = pullback_level
            sl = entry * (1 - SL_PCT)
            target = entry * (1 + TARGET_PCT)
            entry_time = c["date"].strftime("%H:%M")
            for w in candles[i+1:]:
                if w["low"] <= sl:
                    return entry, sl, target, "LOSS (SL hit)", entry_time, w["date"].strftime("%H:%M")
                if w["high"] >= target:
                    return entry, sl, target, "WIN (target hit)", entry_time, w["date"].strftime("%H:%M")
            return entry, sl, target, "still open at day end", entry_time, "-"
    return None, None, None, f"setup not completed (stage {stage})", None, None

rows = []
wins = losses = 0
for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        hist = kc.historical_data(tok, today - datetime.timedelta(days=7), today - datetime.timedelta(days=1), "day")
        prev_close = hist[-1]["close"]
        candles = kc.historical_data(tok, today, today, "minute")
        if not candles:
            continue
        # running momentum, any time of day — qualifies if chgPct vs prevClose
        # has reached the threshold at any point up to now
        day_high_so_far = max(c["high"] for c in candles)
        chg = (day_high_so_far - prev_close) / prev_close * 100
        if chg < CHG_FILTER:
            continue
        entry, sl, target, result, entry_time, exit_time = simulate(candles)
        rows.append((sym, chg, entry, sl, target, entry_time, result, exit_time))
        if "WIN" in result: wins += 1
        elif "LOSS" in result: losses += 1
    except Exception as e:
        print(f"{sym:<12} ERROR: {e}")

print(f"{'SYM':<12}{'chg%':>7}{'entry':>9}{'SL':>9}{'target':>9}  {'entry@':>8}{'exit@':>8}   result")
for r in rows:
    sym, chg, entry, sl, target, entry_time, result, exit_time = r
    if entry is None:
        print(f"{sym:<12}{chg:>6.1f}%{'':>9}{'':>9}{'':>9}  {'':>8}{'':>8}   {result}")
    else:
        print(f"{sym:<12}{chg:>6.1f}%{entry:>9.1f}{sl:>9.1f}{target:>9.1f}  {entry_time:>8}{exit_time:>8}   {result}")
print(f"\nTotal today: {wins} wins, {losses} losses")
