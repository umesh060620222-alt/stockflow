"""Buy-momentum strategy, causal end to end: track TODAY's own live chgPct vs
prevClose (no yesterday-based lookup). Once chgPct reaches +2% intraday, that's
the live "buying momentum" signal — start tracking the running low from that
moment, enter once price bounces 0.1% off it, target +0.3%, SL -0.3%."""
import datetime
from collections import defaultdict
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

TARGET_PCT = 0.003
SL_PCT = 0.003
MOMENTUM_THRESHOLD = 2.0  # % intraday move vs prevClose that counts as "buying momentum"
DAYS = 30
TD_PULLBACK = 0.001  # matches the live app's real-time entry logic

kc = Z.kite()
today = datetime.date.today()
start = today - datetime.timedelta(days=DAYS*365//252 + 20)

def simulate_day(candles, prev_close):
    # causal: momentum is confirmed the moment today's own chgPct (vs prevClose)
    # first reaches the threshold — not a lookup into yesterday's close-to-close
    # change. Low tracking starts from that moment, not from market open.
    momentum_reached = False
    low = None
    entry = None
    entry_idx = None
    for i, c in enumerate(candles):
        if not momentum_reached:
            chg = (c["high"] - prev_close) / prev_close * 100
            if chg >= MOMENTUM_THRESHOLD:
                momentum_reached = True
                low = c["low"]
            continue
        if c["low"] < low:
            low = c["low"]
            continue
        bounce_level = low * (1 + TD_PULLBACK)
        if c["high"] < bounce_level:
            continue
        entry = bounce_level
        entry_idx = i
        break
    if entry is None:
        return "no entry" if momentum_reached else "no momentum"
    sl = entry * (1 - SL_PCT)
    target = entry * (1 + TARGET_PCT)
    for w in candles[entry_idx+1:]:
        if w["low"] <= sl:
            return "LOSS"
        if w["high"] >= target:
            return "WIN"
    return "open"

totals = defaultdict(lambda: {"WIN":0,"LOSS":0,"open":0,"no entry":0,"no momentum":0})
tested_days = 0

for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        daily = kc.historical_data(tok, start, today - datetime.timedelta(days=1), "day")
        if len(daily) < DAYS + 2:
            print(sym, "not enough daily history"); continue
        lastN = daily[-DAYS:]
        intraday = kc.historical_data(tok, lastN[0]["date"].date(), today - datetime.timedelta(days=1), "minute")
        by_date = defaultdict(list)
        for c in intraday:
            by_date[c["date"].date()].append(c)
        start_idx = len(daily) - DAYS
        for idx in range(start_idx, len(daily)):
            d = daily[idx]
            y = daily[idx-1]   # d's own prevClose for chgPct
            prev_close = y["close"]
            day_candles = by_date.get(d["date"].date(), [])
            if not day_candles:
                continue
            res = simulate_day(day_candles, prev_close)
            totals[sym][res] += 1
            if res in ("WIN","LOSS","open"):
                tested_days += 1
    except Exception as e:
        print(sym, "ERROR", e)

print(f"{'SYM':<12}{'WIN':>5}{'LOSS':>6}{'open':>6}{'no entry':>10}{'no momentum':>13}")
tot_win = tot_loss = tot_noentry = tot_nomom = 0
for sym in SYMS:
    r = totals[sym]
    if sum(r.values()) == 0:
        continue
    tot_win += r["WIN"]; tot_loss += r["LOSS"]; tot_noentry += r["no entry"]; tot_nomom += r.get("no momentum",0)
    print(f"{sym:<12}{r['WIN']:>5}{r['LOSS']:>6}{r['open']:>6}{r['no entry']:>10}{r.get('no momentum',0):>13}")
print(f"\nTotal: {tot_win} wins, {tot_loss} losses (trades: {tested_days}, "
      f"momentum-reached-but-no-bounce: {tot_noentry}, days never hitting +{MOMENTUM_THRESHOLD}%: {tot_nomom})")
