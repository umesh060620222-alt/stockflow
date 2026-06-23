"""Orchestrate: fetch today's bars -> compute indicators -> replay -> report.

  python run.py            # fetch fresh from yfinance, run, print report
  python run.py --cached   # reuse the last fetch (offline, for tuning thresholds)
  python run.py --csv      # also dump trades to trades.csv
"""
from __future__ import annotations
import sys, json
import config, data as D, strategy as S, engine as E


def main():
    cached = "--cached" in sys.argv
    raw = D.load_cached() if cached else D.fetch()
    if config.BENCHMARK not in raw:
        print(f"WARNING: benchmark {config.BENCHMARK} missing — RS will be 0")
    bench = raw.get(config.BENCHMARK)
    bench_ret = None
    if bench is not None:
        bench_ret = bench.groupby("date")["close"].transform(lambda s: s / s.iloc[0] - 1.0)

    prepared = {}
    for sym, df in raw.items():
        if sym == config.BENCHMARK:
            continue
        if df is None or df.empty:
            continue
        prepared[sym] = S.add_indicators(df, bench_ret)

    if not prepared:
        print("No symbol data fetched. Market data may be unavailable right now.")
        return

    sessions = sorted({d for df in prepared.values() for d in df["date"].unique()})
    print(f"Loaded {len(prepared)} symbols | interval={config.INTERVAL} | "
          f"sessions: {sessions[0]} -> {sessions[-1]}")

    result = E.run(prepared)
    s = result["summary"]
    print("\n===== PAPER RESULT (net of costs) =====")
    if result["n"] == 0:
        print("No signals fired with current thresholds. Loosen VOL_MULT / RS_MIN in config.py.")
        return
    for k, v in s.items():
        print(f"  {k:>16}: {v}")

    print("\n  --- first 12 trades ---")
    for t in result["trades"][:12]:
        print(f"  {t['entry_ts'].strftime('%H:%M')} {t['symbol']:<14} {t['side']:<5} "
              f"{t['reason']:<10} {t['net_pct']:+.2f}%  ({t['minutes']}m)")

    verdict = "EDGE (+EV after costs)" if s["expectancy_pct"] > 0 else "NO EDGE (-EV after costs)"
    print(f"\n  VERDICT: {verdict}  | expectancy {s['expectancy_pct']:+.4f}% per trade\n")

    if "--csv" in sys.argv:
        import csv
        with open("trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=result["trades"][0].keys())
            w.writeheader(); w.writerows(result["trades"])
        print("  wrote trades.csv")


if __name__ == "__main__":
    main()
