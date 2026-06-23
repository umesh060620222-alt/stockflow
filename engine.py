"""Time-stepped portfolio replay: walk every bar in chronological order, manage
open paper positions, fire new entries through the risk gates, and record trades.

This mirrors how a live engine would behave minute-by-minute — the only swap to
go live is feeding bars from a WebSocket instead of from the cached frame.
"""
from __future__ import annotations
import pandas as pd
import config
import strategy as S


def _minutes(a, b) -> float:
    return (a - b).total_seconds() / 60.0


def run(data: dict) -> dict:
    """data: {symbol: indicator-augmented DataFrame}. Returns trades + summary."""
    syms = [s for s in config.UNIVERSE if s in data]
    frames = {s: data[s] for s in syms}
    # global ordered clock across all symbols
    all_ts = sorted(set().union(*[set(df.index) for df in frames.values()]))

    positions = {}        # sym -> dict(side, entry_price, entry_ts)
    cooldown = {}         # sym -> ts until which we won't re-enter
    trades = []
    daily_pnl = {}        # date -> cumulative net pnl fraction
    halted_days = set()

    for ts in all_ts:
        day = ts.date()
        # ---- 1) manage exits first ----
        for sym in list(positions):
            df = frames[sym]
            if ts not in df.index:
                continue
            bar = df.loc[ts]
            pos = positions[sym]
            held = _minutes(ts, pos["entry_ts"])
            do_exit, fill, reason = S.check_exit(pos["side"], pos["entry_price"],
                                                 pos["entry_ts"], bar, held)
            if do_exit:
                gross = S.gross_pct(pos["side"], pos["entry_price"], fill)
                net = S.net_pct(gross)
                trades.append({
                    "symbol": sym, "side": pos["side"], "entry_ts": pos["entry_ts"],
                    "exit_ts": ts, "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(fill, 2), "minutes": round(held, 1),
                    "reason": reason, "gross_pct": round(gross * 100, 3),
                    "net_pct": round(net * 100, 3),
                    "net_inr": round(net * config.CAPITAL_PER_TRADE, 1),
                })
                daily_pnl[day] = daily_pnl.get(day, 0.0) + net
                cooldown[sym] = ts + pd.Timedelta(minutes=config.COOLDOWN_MIN)
                del positions[sym]

        # ---- 2) day-level loss gate ----
        if daily_pnl.get(day, 0.0) <= -config.DAILY_LOSS_LIMIT_PCT:
            halted_days.add(day)

        # ---- 3) fire new entries ----
        if day in halted_days:
            continue
        for sym in syms:
            if len(positions) >= config.MAX_POSITIONS:
                break
            if sym in positions:
                continue
            if sym in cooldown and ts < cooldown[sym]:
                continue
            df = frames[sym]
            if ts not in df.index:
                continue
            row = df.loc[ts]
            sig = S.entry_signal(row)
            if sig:
                # fill at the bar close (the price we'd act on after the bar prints)
                positions[sym] = {"side": sig, "entry_price": float(row["close"]),
                                  "entry_ts": ts}

    return summarize(trades)


def summarize(trades: list) -> dict:
    df = pd.DataFrame(trades)
    if df.empty:
        return {"trades": [], "n": 0, "summary": {"note": "no signals fired"}}
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] <= 0]
    n = len(df)
    summary = {
        "n_trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "avg_win_pct": round(wins["net_pct"].mean(), 3) if len(wins) else 0.0,
        "avg_loss_pct": round(losses["net_pct"].mean(), 3) if len(losses) else 0.0,
        "net_total_pct": round(df["net_pct"].sum(), 3),
        "net_total_inr": round(df["net_inr"].sum(), 1),
        "gross_total_pct": round(df["gross_pct"].sum(), 3),
        "avg_minutes": round(df["minutes"].mean(), 1),
        "expectancy_pct": round(df["net_pct"].mean(), 4),
        "by_reason": df["reason"].value_counts().to_dict(),
        "longs": int((df["side"] == "long").sum()),
        "shorts": int((df["side"] == "short").sum()),
    }
    return {"trades": trades, "n": n, "summary": summary}
