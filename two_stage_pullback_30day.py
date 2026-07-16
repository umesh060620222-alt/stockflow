"""The best-performing variant from today's testing: fixed 0.3% first pullback
(becomes the entry level), fixed 0.1% second pullback off the recovery peak
(confirmation), enter AT the first pullback's price once confirmed. Run across
the last 30 trading days per stock, filtered to days where the stock showed
buying momentum (chgPct vs prevClose >= 0.1%) at some point."""
import datetime
from collections import defaultdict
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

CHG_FILTER = 0.1
PULLBACK_1 = 0.003
PULLBACK_2 = 0.001
SL_PCT = 0.003
TARGET_PCT = 0.003
DAYS = 30

kc = Z.kite()
today = datetime.date.today()
start = today - datetime.timedelta(days=DAYS*365//252 + 20)

def simulate(candles):
    peak1 = None
    level1 = None
    stage = 1
    peak2 = None
    for i, c in enumerate(candles):
        if stage == 1:
            if peak1 is None or c["high"] > peak1:
                peak1 = c["high"] if peak1 is None else max(peak1, c["high"])
                continue
            pullback_level = peak1 * (1 - PULLBACK_1)
            if c["low"] > pullback_level:
                continue
            level1 = pullback_level
            stage = 2
            peak2 = c["high"]
            continue
        if stage == 2:
            if c["high"] > peak2:
                peak2 = c["high"]
                continue
            confirm_level = peak2 * (1 - PULLBACK_2)
            if c["low"] > confirm_level:
                continue
            stage = 3
        if stage == 3:
            if c["high"] < level1:
                continue
            entry = level1
            sl = entry * (1 - SL_PCT)
            target = entry * (1 + TARGET_PCT)
            for w in candles[i+1:]:
                if w["low"] <= sl:
                    return "LOSS"
                if w["high"] >= target:
                    return "WIN"
            return "open"
    return "no entry"

totals = defaultdict(lambda: {"WIN":0,"LOSS":0,"open":0,"no entry":0})
tested = 0

for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        daily = kc.historical_data(tok, start, today - datetime.timedelta(days=1), "day")
        if len(daily) < DAYS + 1:
            print(sym, "not enough daily history"); continue
        lastN = daily[-DAYS:]
        intraday = kc.historical_data(tok, lastN[0]["date"].date(), today - datetime.timedelta(days=1), "minute")
        by_date = defaultdict(list)
        for c in intraday:
            by_date[c["date"].date()].append(c)
        start_idx = len(daily) - DAYS
        for idx in range(start_idx, len(daily)):
            d = daily[idx]
            y = daily[idx-1]
            prev_close = y["close"]
            day_candles = by_date.get(d["date"].date(), [])
            if not day_candles:
                continue
            day_high = max(c["high"] for c in day_candles)
            chg = (day_high - prev_close) / prev_close * 100
            if chg < CHG_FILTER:
                continue
            res = simulate(day_candles)
            totals[sym][res] += 1
            if res in ("WIN","LOSS","open"):
                tested += 1
    except Exception as e:
        print(sym, "ERROR", e)

print(f"{'SYM':<12}{'WIN':>5}{'LOSS':>6}{'open':>6}{'no entry':>10}")
tot_win = tot_loss = 0
for sym in SYMS:
    r = totals[sym]
    if sum(r.values()) == 0:
        continue
    tot_win += r["WIN"]; tot_loss += r["LOSS"]
    print(f"{sym:<12}{r['WIN']:>5}{r['LOSS']:>6}{r['open']:>6}{r['no entry']:>10}")
print(f"\nTotal: {tot_win} wins, {tot_loss} losses (trades: {tested})")
