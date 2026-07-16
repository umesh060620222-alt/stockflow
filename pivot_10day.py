"""Same R1-SELL simulation (entry=R1*1.003, SL=0.5%, target=0.3%) but across the
last 10 trading days per stock, using each day's own prior-day pivot."""
import datetime
from collections import defaultdict
import zerodha as Z
import scanner as SC

SYMS = [s for s in SC.TRADING_LIST if s not in ("HCLTECH","WIPRO","TECHM","TATAMOTORS")]

SL_PCT = 0.005
TARGET_PCT = 0.003
OFFSET_RATE_PER_100 = 0.0001  # 0.01% offset per ₹100 of price

DAYS = 10

kc = Z.kite()
today = datetime.date.today()
start = today - datetime.timedelta(days=DAYS*365//252 + 15)

def simulate(candles, r1):
    # entry = R1 + a price-scaled offset (0.01% per ₹100 of price)
    entry_offset = OFFSET_RATE_PER_100 * (r1 / 100)
    entry = r1 * (1 + entry_offset)
    sl = entry * (1 + SL_PCT)
    target = entry * (1 - TARGET_PCT)
    for i, c in enumerate(candles):
        if c["high"] < entry:
            continue
        for w in candles[i:]:
            if w["high"] >= sl:
                return "LOSS"
            if w["low"] <= target:
                return "WIN"
        return "open"
    return "not tested"

detail = []
totals = defaultdict(lambda: {"WIN":0,"LOSS":0,"open":0,"not tested":0})

for sym in SYMS:
    try:
        q = kc.ltp(f"NSE:{sym}")[f"NSE:{sym}"]
        tok = q["instrument_token"]
        daily = kc.historical_data(tok, start, today - datetime.timedelta(days=1), "day")
        if len(daily) < DAYS+1:
            print(sym, "not enough daily history"); continue
        lastN = daily[-DAYS:]
        intraday = kc.historical_data(tok, lastN[0]["date"].date(), today - datetime.timedelta(days=1), "minute")
        by_date = defaultdict(list)
        for c in intraday:
            by_date[c["date"].date()].append(c)
        start_idx = len(daily) - DAYS
        for idx in range(start_idx, len(daily)):
            y = daily[idx-1]
            d = daily[idx]
            H,L,C = y["high"], y["low"], y["close"]
            PP=(H+L+C)/3; R1=2*PP-L
            day_candles = by_date.get(d["date"].date(), [])
            if not day_candles:
                continue
            res = simulate(day_candles, R1)
            totals[sym][res]+=1
            detail.append((sym, d["date"].date(), round(R1,1), res))
    except Exception as e:
        print(sym, "ERROR", e)

print(f"{'SYM':<12}{'WIN':>5}{'LOSS':>6}{'open':>6}{'not tested':>12}")
tot_win=tot_loss=0
for sym in SYMS:
    r = totals[sym]
    tot_win+=r['WIN']; tot_loss+=r['LOSS']
    print(f"{sym:<12}{r['WIN']:>5}{r['LOSS']:>6}{r['open']:>6}{r['not tested']:>12}")
print(f"\nTotal: {tot_win} wins, {tot_loss} losses across all stocks/days")
