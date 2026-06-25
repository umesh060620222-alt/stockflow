"""Early-session pick selector — run LOCALLY with Zerodha connected.

Railway computes the day's top 5 (relative-strength leaders) off yesterday's
close, available any time before 9 AM at {RAILWAY_URL}/api/recommend. Run this
~7 minutes after the open: it checks those 5 live in Kite and picks the
highest-ranked GREEN one (up on the day). Green is the signal — no volume math.

    set RAILWAY_URL=https://your-app.up.railway.app   # or edit DEFAULT_URL
    python login.py          # once per day — mints today's Kite token
    python early_pick.py     # at ~9:22 IST
"""
from __future__ import annotations
import os, sys, json, urllib.request
import zerodha as Z

DEFAULT_URL = os.getenv("RAILWAY_URL", "http://127.0.0.1:8000")


def top5(market="IN"):
    url = f"{DEFAULT_URL}/api/recommend?market={market}"
    d = json.load(urllib.request.urlopen(url, timeout=30))
    return [p["symbol"] for p in d.get("picks", [])]


def main(market="IN"):
    syms = top5(market)
    if not syms:
        print("No picks returned from Railway — is the URL right / market valid?")
        return
    kc = Z.kite()                                   # raises if no token (run login.py)
    keys = [f"NSE:{Z._ksym(s)}" for s in syms]
    q = kc.quote(keys)

    print(f"\n{'rk':<4}{'symbol':<12}{'last':>9}{'prev':>9}{'chg%':>8}   state")
    print("-" * 50)
    pick = None
    for i, s in enumerate(syms, 1):
        d = q.get(f"NSE:{Z._ksym(s)}", {})
        last = d.get("last_price")
        prev = (d.get("ohlc") or {}).get("close")
        if last is None or not prev:
            print(f"{i:<4}{Z._ksym(s):<12}{'-':>9}{'-':>9}{'-':>8}   no data")
            continue
        chg = (last / prev - 1) * 100
        green = last > prev
        if green and pick is None:
            pick = s                                # highest-ranked green name wins
        print(f"{i:<4}{Z._ksym(s):<12}{last:>9.1f}{prev:>9.1f}{chg:>+7.2f}%   {'GREEN' if green else 'red'}")

    print("-" * 50)
    if pick:
        print(f">>> PICK: {Z._ksym(pick)}  (highest-ranked green of the top 5)\n")
    else:
        print(">>> No green name in the top 5 — sit out today.\n")


if __name__ == "__main__":
    main(sys.argv[1].upper() if len(sys.argv) > 1 else "IN")
