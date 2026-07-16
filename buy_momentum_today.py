"""If yesterday ended strongly up (+2% or more), that's a buying signal — no
shorting it. For those stocks, find the low of today's first 15 minutes
(09:15-09:29), buy there, and check whether +0.3% (WIN) or -0.3% (SL/LOSS)
comes first at any point during the rest of the day."""
import datetime
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

TARGET_PCT = 0.003
SL_PCT = 0.003
MIN_PREV_DAY_CHG = 2.0  # %

kc = Z.kite()
today = datetime.date.today()

rows = []
wins = losses = 0
for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        hist = kc.historical_data(tok, today - datetime.timedelta(days=12), today - datetime.timedelta(days=1), "day")
        target_day = hist[-1]   # the day we're testing (most recent completed trading day)
        y = hist[-2]             # target_day's own "yesterday"
        y2 = hist[-3]            # day before that, to compute y's % change
        chg = (y["close"] - y2["close"]) / y2["close"] * 100
        if chg < MIN_PREV_DAY_CHG:
            continue
        d = target_day["date"].date()
        candles = kc.historical_data(tok, d, d, "minute")
        first15 = [c for c in candles if c["date"].strftime("%H:%M") < "09:30"]
        if not first15:
            rows.append((sym, chg, None, None, None, None, "no data yet"))
            continue
        low_candle = min(first15, key=lambda c: c["low"])
        entry = low_candle["low"]
        entry_time = low_candle["date"].strftime("%H:%M")
        sl = entry * (1 - SL_PCT)
        target = entry * (1 + TARGET_PCT)
        result, exit_time = "still open", "-"
        start_idx = candles.index(low_candle)
        # start AFTER the low candle — same same-candle-ordering ambiguity as before
        for w in candles[start_idx+1:]:
            if w["low"] <= sl:
                result, exit_time = "LOSS (SL hit)", w["date"].strftime("%H:%M")
                break
            if w["high"] >= target:
                result, exit_time = "WIN (target hit)", w["date"].strftime("%H:%M")
                break
        rows.append((sym, chg, entry, sl, target, entry_time, result, exit_time))
        if "WIN" in result: wins += 1
        elif "LOSS" in result: losses += 1
    except Exception as e:
        print(f"{sym:<12} ERROR: {e}")

print(f"{'SYM':<12}{'prevChg':>9}{'entry':>9}{'SL':>9}{'target':>9}  {'entry@':>8}{'exit@':>8}   result")
for r in rows:
    if len(r) == 7:
        sym, chg, entry, sl, target, entry_time, result = r
        print(f"{sym:<12}{chg:>8.1f}%{'':>9}{'':>9}{'':>9}  {'':>8}{'':>8}   {result}")
    else:
        sym, chg, entry, sl, target, entry_time, result, exit_time = r
        print(f"{sym:<12}{chg:>8.1f}%{entry:>9.1f}{sl:>9.1f}{target:>9.1f}  {entry_time:>8}{exit_time:>8}   {result}")
print(f"\nTotal (prev trading day): {wins} wins, {losses} losses")
