"""If a stock opens 1%+ above yesterday's close (gap-up, buying interest at
open), track the running peak from 09:15 onward and buy once price pulls back
0.1% off that peak (the initial momentum fading) — betting on continuation,
not the fade itself. Target +0.3%, SL -0.3%. Run for today only."""
import datetime
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

GAP_PCT = 0.01     # 1% gap-up at open required
TD_PULLBACK = 0.001
SL_PCT = 0.003
TARGET_PCT = 0.003

kc = Z.kite()
today = datetime.date.today()

def simulate(candles):
    # track running peak from 09:15, buy once price pulls back 0.1% off it —
    # a candle that sets/extends the peak can't also confirm its own pullback.
    peak = None
    for i, c in enumerate(candles):
        if peak is None or c["high"] > peak:
            peak = c["high"] if peak is None else max(peak, c["high"])
            continue
        pullback_level = peak * (1 - TD_PULLBACK)
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
    return None, None, None, "no pullback confirmed", None, None

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
        day_open = candles[0]["open"]
        gap = (day_open - prev_close) / prev_close * 100
        if gap < GAP_PCT * 100:
            rows.append((sym, gap, None, None, None, None, "gap too small"))
            continue
        entry, sl, target, result, entry_time, exit_time = simulate(candles)
        rows.append((sym, gap, entry, sl, target, entry_time, result, exit_time))
        if "WIN" in result: wins += 1
        elif "LOSS" in result: losses += 1
    except Exception as e:
        print(f"{sym:<12} ERROR: {e}")

print(f"{'SYM':<12}{'gap%':>7}{'entry':>9}{'SL':>9}{'target':>9}  {'entry@':>8}{'exit@':>8}   result")
for r in rows:
    if len(r) == 7:
        sym, gap, entry, sl, target, entry_time, result = r
        print(f"{sym:<12}{gap:>6.1f}%{'':>9}{'':>9}{'':>9}  {'':>8}{'':>8}   {result}")
    else:
        sym, gap, entry, sl, target, entry_time, result, exit_time = r
        if entry is None:
            print(f"{sym:<12}{gap:>6.1f}%{'':>9}{'':>9}{'':>9}  {'':>8}{'':>8}   {result}")
        else:
            print(f"{sym:<12}{gap:>6.1f}%{entry:>9.1f}{sl:>9.1f}{target:>9.1f}  {entry_time:>8}{exit_time:>8}   {result}")
print(f"\nTotal today: {wins} wins, {losses} losses")
