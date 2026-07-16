"""For every trading day in the past ~1 year: compute the top pick as of that
day, mark POSITIVE if its High > Open that day else NEGATIVE, print each day,
save to a file, and report total positives / negatives.

    python toppick_highopen.py IN      # or US
"""
from __future__ import annotations
import sys
import pandas as pd
import backtest_picks as BT
import recommend as REC


def top_pick(close, vol, bench, it):
    cand = BT.rank_asof(close, vol, bench, it)
    if not cand.empty:
        return cand["symbol"].iloc[0]
    # fallback so there's always a pick: highest RS overall (filters ignored)
    bc = close[bench]
    bret = bc.iloc[it] / bc.iloc[it - 21] - 1
    best, best_rs = None, -1e9
    for s in close.columns:
        if s == bench:
            continue
        c = close[s]
        if pd.isna(c.iloc[it]) or pd.isna(c.iloc[it - 21]):
            continue
        rs = (c.iloc[it] / c.iloc[it - 21] - 1) - bret
        if rs > best_rs:
            best_rs, best = rs, s
    return best


def run(market="IN", year_days=252):
    m = REC.MARKETS.get(market, REC.MARKETS["IN"])
    close, vol, op, hi = BT.fetch(m["universe"] + [m["bench"]])
    bench = m["bench"]
    n = len(close)
    dates = close.index
    start = max(64, n - year_days)

    lines, pos, neg = [], 0, 0
    for it in range(start, n):
        s0 = top_pick(close, vol, bench, it)
        name = s0.replace(".NS", "") if s0 else "—"
        o, h = (op[s0].iloc[it], hi[s0].iloc[it]) if s0 else (None, None)
        if pd.notna(o) and pd.notna(h) and h > o:
            mark, pos = "POSITIVE", pos + 1
        else:
            mark, neg = "NEGATIVE", neg + 1
        line = f"{dates[it].date()}  {name:14} {mark}"
        print(line)
        lines.append(line)

    total = pos + neg
    summary = (f"\nTOTAL POSITIVES: {pos}\nTOTAL NEGATIVES: {neg}\n"
               f"DAYS: {total}  ·  POSITIVE %: {pos/total*100:.1f}%")
    print(summary)
    out = f"toppick_highopen_{market}.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n" + summary + "\n")
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    run(sys.argv[1].upper() if len(sys.argv) > 1 else "IN")
