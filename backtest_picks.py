"""Walk-forward backtest of the daily relative-strength picks over ~1 year.

Reconstructs the pick at each rebalance using ONLY data available up to that
date (no look-ahead), holds to the next rebalance, and compares to the
benchmark. Covers the testable technical core — RS vs benchmark + 50-DMA trend
+ volume confirmation. Fundamentals (PE/EPS) and the news veto are live-only and
can't be rebuilt from history, so they're omitted here (results are the
technical screen alone). Returns use split/div-adjusted closes.

    python backtest_picks.py IN          # or US
"""
from __future__ import annotations
import sys
import pandas as pd, numpy as np
import yfinance as yf
import config, recommend as REC

COST = config.COST_PCT + 2 * config.SLIPPAGE_PCT      # round-trip cost charged per held position


def fetch(symbols, period="450d"):
    raw = yf.download(symbols, period=period, interval="1d", auto_adjust=True,
                      progress=False, threads=True)
    close = raw["Close"].dropna(how="all")
    vol = raw["Volume"].reindex(close.index)
    op = raw["Open"].reindex(close.index)
    hi = raw["High"].reindex(close.index)
    return close, vol, op, hi


def rank_asof(close, vol, bench_sym, it):
    """Replicate the live RS metrics using data up to (and including) index it."""
    bc = close[bench_sym]
    bret = bc.iloc[it] / bc.iloc[it - 21] - 1
    rows = []
    for s in close.columns:
        if s == bench_sym:
            continue
        c, v = close[s], vol[s]
        if pd.isna(c.iloc[it]) or pd.isna(c.iloc[it - 21]):
            continue
        r1m = c.iloc[it] / c.iloc[it - 21] - 1
        dma50 = c.iloc[it - 49:it + 1].mean()
        v0, v1, v2, v3 = v.iloc[it - 3], v.iloc[it - 2], v.iloc[it - 1], v.iloc[it]
        rising3 = pd.notna([v0, v1, v2, v3]).all() and (v0 < v1 < v2 < v3)
        rows.append({"symbol": s, "rs": (r1m - bret) * 100,
                     "above_50dma": c.iloc[it] > dma50,
                     "vol_ok": bool(rising3)})
    df = pd.DataFrame(rows)
    cand = df[(df["above_50dma"]) & (df["rs"] > 0) & (df["vol_ok"])].sort_values("rs", ascending=False)
    return cand


def backtest(market="IN", hold=21, topn=1, year_days=252):
    m = REC.MARKETS.get(market, REC.MARKETS["IN"])
    syms = m["universe"] + [m["bench"]]
    close, vol, op, hi = fetch(syms)
    bench = m["bench"]
    n = len(close)
    start = max(64, n - year_days)            # ~1y window, leave lookback room
    dates = close.index

    rows, pick_log = [], []
    hi_open_same = hi_open_next = 0           # days the top pick traded above its open
    it = start
    while it + hold < n:
        cand = rank_asof(close, vol, bench, it)
        picks = cand["symbol"].head(topn).tolist()
        if picks:
            s0 = picks[0]
            if pd.notna(hi[s0].iloc[it]) and pd.notna(op[s0].iloc[it]) and hi[s0].iloc[it] > op[s0].iloc[it]:
                hi_open_same += 1             # pick day itself (the after-close signal day)
            j = it + 1                        # next session = first day you could actually act
            if j < n and pd.notna(hi[s0].iloc[j]) and pd.notna(op[s0].iloc[j]) and hi[s0].iloc[j] > op[s0].iloc[j]:
                hi_open_next += 1
        b0, b1 = close[bench].iloc[it], close[bench].iloc[it + hold]
        bench_ret = (b1 / b0 - 1) * 100
        if picks:
            rets = []
            for s in picks:
                p0, p1 = close[s].iloc[it], close[s].iloc[it + hold]
                if pd.notna(p0) and pd.notna(p1):
                    rets.append((p1 / p0 - 1) * 100 - COST * 100)
            pick_ret = float(np.mean(rets)) if rets else 0.0
        else:
            pick_ret = 0.0                    # no qualifying pick -> sit in cash (flat)
        rows.append({"date": str(dates[it].date()), "pick_ret": pick_ret,
                     "bench_ret": bench_ret, "excess": pick_ret - bench_ret,
                     "top": picks[0].replace(".NS", "") if picks else "—"})
        pick_log.append((str(dates[it].date()), rows[-1]["top"], round(pick_ret, 2), round(bench_ret, 2)))
        it += hold

    r = pd.DataFrame(rows)
    if r.empty:
        print("No periods — insufficient history."); return
    cum_pick = (1 + r["pick_ret"] / 100).prod() - 1
    cum_bench = (1 + r["bench_ret"] / 100).prod() - 1
    wins = (r["pick_ret"] > 0).mean() * 100
    beat = (r["excess"] > 0).mean() * 100

    print(f"\n=== {market} pick backtest — top {topn}, hold {hold}d, {len(r)} periods "
          f"({r['date'].iloc[0]} → {r['date'].iloc[-1]}) ===")
    print(f"  cumulative pick return : {cum_pick*100:+.1f}%")
    print(f"  cumulative benchmark   : {cum_bench*100:+.1f}%  ({m['bench_name']})")
    print(f"  avg per period         : pick {r['pick_ret'].mean():+.2f}%  vs bench {r['bench_ret'].mean():+.2f}%")
    print(f"  win rate (pick > 0)    : {wins:.0f}%")
    print(f"  beat benchmark         : {beat:.0f}% of periods")
    print(f"  best / worst period    : {r['pick_ret'].max():+.1f}% / {r['pick_ret'].min():+.1f}%")
    np_ = len(r)
    print(f"  top pick High>Open     : same session {hi_open_same}/{np_} ({hi_open_same/np_*100:.0f}%)  ·  "
          f"next session {hi_open_next}/{np_} ({hi_open_next/np_*100:.0f}%)")
    print(f"  (round-trip cost charged: {COST*100:.2f}% per position per period)")
    print("\n  period picks (date, top, pick%, bench%):")
    for d, t, pr, br in pick_log:
        print(f"    {d}  {t:14} {pr:+6.2f}%   (bench {br:+5.2f}%)")


if __name__ == "__main__":
    mkt = (sys.argv[1].upper() if len(sys.argv) > 1 else "IN")
    for hold in (5, 21):                  # 5-day (as the track record grades) + monthly
        for topn in (1, 5):
            backtest(mkt, hold=hold, topn=topn)
