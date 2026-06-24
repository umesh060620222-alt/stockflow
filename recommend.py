"""Daily recommendation engine.

Once a day (after close) it ranks a broad universe by RELATIVE STRENGTH vs the
Nifty over ~1 month — i.e. where money is actually flowing — keeps only names in
a healthy uptrend, attaches recent news headlines for context, and records the
top pick so the next day can VERIFY whether it moved as expected.

Relative-strength leadership is a documented, slow, retail-tradeable edge — the
opposite end of the spectrum from the intraday microstructure HFT owns.
"""
from __future__ import annotations
import os, json, datetime as dt
import pandas as pd, numpy as np
import config

REC_FILE = os.path.join(os.path.dirname(__file__), "recommendations.json")

# broad liquid universe (large + mid caps). Expandable; small-caps need a longer list.
BROAD = [
    "RELIANCE.NS","HDFCBANK.NS","ICICIBANK.NS","INFY.NS","TCS.NS","SBIN.NS","AXISBANK.NS",
    "KOTAKBANK.NS","LT.NS","ITC.NS","BHARTIARTL.NS","HINDUNILVR.NS","BAJFINANCE.NS","MARUTI.NS",
    "SUNPHARMA.NS","TITAN.NS","ADANIENT.NS","WIPRO.NS","HCLTECH.NS","NTPC.NS","POWERGRID.NS",
    "TATASTEEL.NS","JSWSTEEL.NS","ULTRACEMCO.NS","NESTLEIND.NS","BAJAJFINSV.NS","ONGC.NS",
    "COALINDIA.NS","M&M.NS","TECHM.NS","ADANIPORTS.NS","GRASIM.NS","HINDALCO.NS","CIPLA.NS",
    "DRREDDY.NS","BRITANNIA.NS","EICHERMOT.NS","DIVISLAB.NS","HEROMOTOCO.NS","BPCL.NS",
    "GAIL.NS","SHRIRAMFIN.NS","DLF.NS","SIEMENS.NS","PIDILITIND.NS","HAL.NS","BEL.NS",
    "TRENT.NS","VBL.NS","ZOMATO.NS","JUBLFOOD.NS","INDIGO.NS","BANKBARODA.NS","PNB.NS",
    "IRFC.NS","IRCTC.NS","TATAPOWER.NS","ADANIGREEN.NS","PERSISTENT.NS","MAZDOCK.NS",
]

# US large-cap universe (NYSE/Nasdaq) — yfinance uses the bare ticker, no suffix.
US_BROAD = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","BRK-B","JPM","V","UNH","XOM","JNJ",
    "WMT","MA","PG","HD","CVX","ABBV","KO","PEP","COST","MRK","AVGO","LLY","BAC","PFE","TMO",
    "CSCO","MCD","ABT","DIS","ADBE","CRM","NKE","INTC","AMD","QCOM","TXN","NFLX","ORCL","IBM",
    "GE","CAT","BA","GS","AXP","SBUX","UBER","C","T","VZ","PM","HON","LOW","INTU","SPGI",
]

MARKETS = {
    "IN": {"universe": BROAD,    "bench": "^NSEI", "cur": "₹", "bench_name": "Nifty"},
    "US": {"universe": US_BROAD, "bench": "^GSPC", "cur": "$",      "bench_name": "S&P 500"},
}


def _daily_closes(symbols, days=120):
    import yfinance as yf
    raw = yf.download(symbols, period=f"{days}d", interval="1d", group_by="ticker",
                      progress=False, threads=True, auto_adjust=False)
    out = {}
    for s in symbols:
        try:
            df = raw[s] if len(symbols) > 1 else raw
            c = df["Close"].dropna()
            if len(c) > 25:
                out[s] = c
        except Exception:
            pass
    return out


def relative_strength(universe, bench_sym, days=120):
    data = _daily_closes(universe + [bench_sym], days)
    bench = data.get(bench_sym)
    bret = float(bench.iloc[-1] / bench.iloc[-21] - 1) if bench is not None and len(bench) > 21 else 0.0
    rows = []
    for s, c in data.items():
        if s == bench_sym:
            continue
        r1m = float(c.iloc[-1] / c.iloc[-21] - 1)
        r3m = float(c.iloc[-1] / c.iloc[-min(63, len(c) - 1)] - 1)
        rows.append({
            "symbol": s, "price": round(float(c.iloc[-1]), 2),
            "ret_1m": round(r1m * 100, 1), "ret_3m": round(r3m * 100, 1),
            "rs_vs_nifty": round((r1m - bret) * 100, 1),
            "above_50dma": bool(c.iloc[-1] > c.tail(50).mean()),
            "near_high": bool(c.iloc[-1] >= c.tail(60).max() * 0.97),
        })
    df = pd.DataFrame(rows).sort_values("rs_vs_nifty", ascending=False).reset_index(drop=True)
    return df, round(bret * 100, 1), data


def news_headlines(symbol, n=4):
    import requests, xml.etree.ElementTree as ET
    q = symbol.replace(".NS", "") + " stock NSE"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        items = ET.fromstring(r.content).findall(".//item")[:n]
        return [it.find("title").text for it in items]
    except Exception:
        return []


def fundamentals(symbol):
    """Trailing P/E and EPS from yfinance (one .info call). None if unavailable."""
    import yfinance as yf
    try:
        info = yf.Ticker(symbol).info
        pe = info.get("trailingPE") or info.get("forwardPE")
        eps = info.get("trailingEps")
        return {"pe": round(float(pe), 1) if pe else None,
                "eps": round(float(eps), 2) if eps is not None else None}
    except Exception:
        return {"pe": None, "eps": None}


def build_reasons(d, bench_name="Nifty"):
    """Deterministic, always-available explanation of why this stock ranked #1."""
    r = [f"Top by relative strength: +{d['rs_vs_nifty']}% vs {bench_name} over 1 month — "
         f"money is rotating into it while the index lags"]
    trend = "trading above its 50-day average" + (
        ", and at/near its 60-day high" if d.get("near_high") else "")
    r.append(f"Trend confirmed: {trend} (not a falling knife)")
    r.append(f"Momentum: {d['ret_3m']:+}% over 3 months, {d['ret_1m']:+}% over 1 month")
    if d.get("pe") is not None:
        r.append(f"Valuation in check: P/E {d['pe']} (under the 50 cap — not priced for perfection)")
    if d.get("eps") is not None:
        r.append(f"Actually profitable: EPS {d['eps']} (> 0, so the move is backed by earnings)")
    return r


def catalyst_score(symbol, headlines):
    """Use Claude to read the headlines and explain the next-few-days catalyst.
    Returns {direction, conviction, reasoning} or None if no API key / no news."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not headlines:
        return None
    import requests, re
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    prompt = ("You are a sell-side equity analyst. From these recent news headlines for an "
              "Indian (NSE) stock, judge the catalyst for the NEXT FEW DAYS and EXPLAIN your "
              "reasoning. Reply ONLY with JSON: "
              '{"direction":"up|down|neutral","conviction":0-100,'
              '"reasoning":"2 sentences citing the specific news driving your view"}.\n'
              f"Stock: {symbol.replace('.NS','')}\nHeadlines:\n- " + "\n- ".join(headlines))
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        txt = r.json()["content"][0]["text"]
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        return {"error": str(e)[:80]}


def daily_pick(market="IN", top=5, with_news=True, do_record=True):
    m = MARKETS.get(market, MARKETS["IN"])
    df, bench_1m, closes = relative_strength(m["universe"], m["bench"])
    cand = df[(df["above_50dma"]) & (df["rs_vs_nifty"] > 0)].head(top * 3)  # over-select for PE filter
    picks = []
    for _, row in cand.iterrows():
        if len(picks) >= top:
            break
        f = fundamentals(row["symbol"])
        pe, eps = f["pe"], f["eps"]
        if eps is None or eps <= config.REC_MIN_EPS:
            continue                         # loss-making / no earnings -> skip
        if pe is not None and pe > config.REC_PE_MAX:
            continue                         # too expensive -> skip (valuation guard)
        d = row.to_dict(); d["pe"] = pe; d["eps"] = eps
        d["reasons"] = build_reasons(d, m["bench_name"])
        if with_news:
            d["news"] = news_headlines(row["symbol"])
            d["catalyst"] = catalyst_score(row["symbol"], d["news"])
        picks.append(d)
    result = {"date": str(dt.date.today()), "market": market, "currency": m["cur"],
              "bench_name": m["bench_name"], "bench_1m_pct": bench_1m, "picks": picks}

    # record a slim copy (no track/news bloat), then verify prior top-picks vs now
    if do_record:
        record({"date": result["date"], "bench_1m_pct": bench_1m,
                "picks": [{k: p[k] for k in ("symbol", "price", "rs_vs_nifty")} for p in picks]}, market)

    track = []
    for h in load_history(market):
        if h["date"] == result["date"] or not h.get("picks"):
            continue
        t = h["picks"][0]
        if t["symbol"] in closes:
            now = float(closes[t["symbol"]].iloc[-1])
            track.append({"date": h["date"], "symbol": t["symbol"].replace(".NS", ""),
                          "entry": t["price"], "now": round(now, 2),
                          "ret_pct": round((now - t["price"]) / t["price"] * 100, 2)})
    result["track"] = track[-15:]
    if track:
        rets = [t["ret_pct"] for t in track]
        result["track_summary"] = {"n": len(rets), "avg_ret_pct": round(sum(rets) / len(rets), 2),
                                   "win_pct": round(100 * sum(1 for r in rets if r > 0) / len(rets))}
    return result


def _rec_file(market):
    return os.path.join(os.path.dirname(__file__), f"recommendations_{market}.json")


def record(pick, market="IN"):
    path = _rec_file(market)
    hist = json.load(open(path)) if os.path.exists(path) else []
    hist = [h for h in hist if h.get("date") != pick["date"]]  # replace same-day
    hist.append(pick)
    json.dump(hist[-120:], open(path, "w"), indent=2)


def load_history(market="IN"):
    path = _rec_file(market)
    return json.load(open(path)) if os.path.exists(path) else []
