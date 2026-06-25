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
import options as OPT

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

# ticker -> sector/theme, for the "money flowing into <sector>" rollup
SECTORS = {
    # India
    "RELIANCE.NS": "Energy", "ONGC.NS": "Energy", "COALINDIA.NS": "Energy", "BPCL.NS": "Energy", "GAIL.NS": "Energy",
    "NTPC.NS": "Power", "POWERGRID.NS": "Power", "TATAPOWER.NS": "Power", "ADANIGREEN.NS": "Power",
    "HDFCBANK.NS": "Financials", "ICICIBANK.NS": "Financials", "SBIN.NS": "Financials", "AXISBANK.NS": "Financials",
    "KOTAKBANK.NS": "Financials", "BAJFINANCE.NS": "Financials", "BAJAJFINSV.NS": "Financials", "SHRIRAMFIN.NS": "Financials",
    "BANKBARODA.NS": "Financials", "PNB.NS": "Financials", "IRFC.NS": "Financials",
    "INFY.NS": "IT", "TCS.NS": "IT", "WIPRO.NS": "IT", "HCLTECH.NS": "IT", "TECHM.NS": "IT", "PERSISTENT.NS": "IT",
    "MARUTI.NS": "Auto", "M&M.NS": "Auto", "EICHERMOT.NS": "Auto", "HEROMOTOCO.NS": "Auto",
    "TATASTEEL.NS": "Metals", "JSWSTEEL.NS": "Metals", "HINDALCO.NS": "Metals",
    "ITC.NS": "FMCG", "HINDUNILVR.NS": "FMCG", "NESTLEIND.NS": "FMCG", "BRITANNIA.NS": "FMCG", "VBL.NS": "FMCG", "JUBLFOOD.NS": "FMCG",
    "TITAN.NS": "Consumer", "TRENT.NS": "Consumer",
    "SUNPHARMA.NS": "Pharma", "CIPLA.NS": "Pharma", "DRREDDY.NS": "Pharma", "DIVISLAB.NS": "Pharma",
    "LT.NS": "Infra", "ADANIPORTS.NS": "Infra", "ADANIENT.NS": "Infra",
    "SIEMENS.NS": "Capital Goods", "BEL.NS": "Defence", "HAL.NS": "Defence", "MAZDOCK.NS": "Defence",
    "ULTRACEMCO.NS": "Cement", "GRASIM.NS": "Cement", "BHARTIARTL.NS": "Telecom", "DLF.NS": "Realty",
    "PIDILITIND.NS": "Chemicals", "INDIGO.NS": "Aviation", "IRCTC.NS": "Travel",
    # US
    "AAPL": "Big Tech", "MSFT": "Big Tech", "GOOGL": "Big Tech", "META": "Big Tech", "ORCL": "Big Tech",
    "CRM": "Big Tech", "ADBE": "Big Tech", "CSCO": "Big Tech", "IBM": "Big Tech", "INTU": "Big Tech",
    "NVDA": "Chips", "AMD": "Chips", "AVGO": "Chips", "INTC": "Chips", "TXN": "Chips", "QCOM": "Chips",
    "AMZN": "Consumer", "WMT": "Consumer", "HD": "Consumer", "COST": "Consumer", "PG": "Consumer", "KO": "Consumer",
    "PEP": "Consumer", "MCD": "Consumer", "NKE": "Consumer", "SBUX": "Consumer", "LOW": "Consumer", "DIS": "Consumer",
    "NFLX": "Consumer", "UBER": "Consumer", "PM": "Consumer",
    "JPM": "Financials", "V": "Financials", "MA": "Financials", "BAC": "Financials", "GS": "Financials",
    "AXP": "Financials", "C": "Financials", "SPGI": "Financials", "BRK-B": "Financials",
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare", "MRK": "Healthcare", "ABBV": "Healthcare",
    "PFE": "Healthcare", "TMO": "Healthcare", "ABT": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "GE": "Industrials", "CAT": "Industrials", "BA": "Industrials", "HON": "Industrials",
    "T": "Telecom", "VZ": "Telecom", "TSLA": "Auto/EV",
}


def sector_rollup(df, top=4):
    """Aggregate the uptrending leaders by sector -> where money is flowing today,
    with the constituent stock names per sector (strongest first)."""
    d = df.copy()
    d["sector"] = d["symbol"].map(SECTORS).fillna("Other")
    lead = d[(d["rs_vs_nifty"] > 0) & (d["above_50dma"])].sort_values("rs_vs_nifty", ascending=False)
    if lead.empty:
        return []
    g = lead.groupby("sector").agg(avg_rs=("rs_vs_nifty", "mean"), total=("rs_vs_nifty", "sum"),
                                   n=("symbol", "count")).reset_index()
    g = g.sort_values("total", ascending=False)   # breadth x strength = where money is really flowing
    out = []
    for _, r in g.head(top).iterrows():
        names = (lead[lead["sector"] == r["sector"]]["symbol"]
                 .str.replace(".NS", "", regex=False).tolist())
        out.append({"sector": r["sector"], "avg_rs": round(float(r["avg_rs"]), 1),
                    "n": int(r["n"]), "names": names})
    return out


def _daily_ohlcv(symbols, days=120):
    """Per-symbol DataFrame with Close + Volume (volume confirms the price move)."""
    import yfinance as yf
    raw = yf.download(symbols, period=f"{days}d", interval="1d", group_by="ticker",
                      progress=False, threads=True, auto_adjust=False)
    out = {}
    for s in symbols:
        try:
            df = raw[s] if len(symbols) > 1 else raw
            sub = df[["Close", "Volume"]].dropna()
            if len(sub) > 25:
                out[s] = sub
        except Exception:
            pass
    return out


def _reversal_flags(c, v, above_50dma, ext_pct, vol20):
    """Early-warning signs that an uptrend is about to roll over."""
    rev = []
    if above_50dma and ext_pct > config.REC_MAX_EXT_PCT:
        rev.append(f"overextended (+{ext_pct}% above 50-DMA)")
    if above_50dma and float(c.iloc[-1]) < float(c.tail(10).mean()):
        rev.append("lost the 10-DMA (short-term rollover)")
    last_ret = float(c.iloc[-1] / c.iloc[-2] - 1) * 100
    if last_ret <= -3 and vol20 > 0 and float(v.iloc[-1]) > 1.5 * vol20:
        rev.append("heavy down-day on high volume (distribution)")
    return rev


def relative_strength(universe, bench_sym, days=120):
    data = _daily_ohlcv(universe + [bench_sym], days)
    closes = {s: sub["Close"] for s, sub in data.items()}     # close-only view (track record / options)
    bench = closes.get(bench_sym)
    bret = float(bench.iloc[-1] / bench.iloc[-21] - 1) if bench is not None and len(bench) > 21 else 0.0
    rows = []
    for s, sub in data.items():
        if s == bench_sym:
            continue
        c, v = sub["Close"], sub["Volume"]
        r1m = float(c.iloc[-1] / c.iloc[-21] - 1)
        r3m = float(c.iloc[-1] / c.iloc[-min(63, len(c) - 1)] - 1)
        vol20 = float(v.tail(20).mean())             # still used by the reversal check
        vseq = [float(x) for x in v.tail(4)]         # 4 bars -> the last 3 daily changes
        rising3 = len(vseq) == 4 and vseq[0] < vseq[1] < vseq[2] < vseq[3]
        rvol = round(vseq[-1] / vseq[0], 2) if len(vseq) == 4 and vseq[0] > 0 else 0.0
        dma50 = float(c.tail(50).mean())
        ext_pct = round((float(c.iloc[-1]) / dma50 - 1) * 100, 1) if dma50 > 0 else 0.0
        above_50dma = bool(c.iloc[-1] > dma50)
        rev = _reversal_flags(c, v, above_50dma, ext_pct, vol20)
        rows.append({
            "symbol": s, "price": round(float(c.iloc[-1]), 2),
            "ret_1m": round(r1m * 100, 1), "ret_3m": round(r3m * 100, 1),
            "rs_vs_nifty": round((r1m - bret) * 100, 1),
            "above_50dma": above_50dma,
            "near_high": bool(c.iloc[-1] >= c.tail(60).max() * 0.97),
            "near_low": bool(c.iloc[-1] <= c.tail(60).min() * 1.03),
            "rvol": rvol,                                   # today's vol / 3 days ago
            "vol_ok": bool(rising3),                        # volume higher EACH of the last 3 days
            "ext_pct": ext_pct,                            # % above the 50-DMA
            "rev_risk": bool(rev), "rev_flags": rev,
        })
    df = pd.DataFrame(rows).sort_values("rs_vs_nifty", ascending=False).reset_index(drop=True)
    return df, round(bret * 100, 1), closes


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
    if d.get("vol_ok"):
        r.append(f"Volume rising 3 days straight (today {d['rvol']}× 3 days ago) — "
                 f"participation building into the move")
    if d.get("rev_risk") and d.get("rev_flags"):
        r.append("⚠ Reversal watch: " + "; ".join(d["rev_flags"]) +
                 " — tighten stops / size down")
    return r


def build_sell_reasons(d, bench_name="Nifty"):
    """Deterministic explanation of why this stock ranked as the top SELL/avoid —
    the mirror of build_reasons: weakest relative strength in a confirmed downtrend."""
    r = [f"Weakest by relative strength: {d['rs_vs_nifty']:+}% vs {bench_name} over 1 month — "
         f"money is rotating OUT while the index holds up better"]
    trend = "trading below its 50-day average" + (
        ", and at/near its 60-day low" if d.get("near_low") else "")
    r.append(f"Downtrend confirmed: {trend} (not a dip in an uptrend)")
    r.append(f"Momentum: {d['ret_3m']:+}% over 3 months, {d['ret_1m']:+}% over 1 month")
    if d.get("pe") is not None and d["pe"] > config.REC_PE_MAX:
        r.append(f"Rich valuation into weakness: P/E {d['pe']} (above the {config.REC_PE_MAX} cap) — "
                 f"little support if the slide continues")
    if d.get("eps") is not None and d["eps"] <= config.REC_MIN_EPS:
        r.append(f"Earnings don't back it: EPS {d['eps']} (≤ 0) — the weakness has a fundamental leg")
    if d.get("vol_ok"):
        r.append(f"Volume rising into the decline 3 days straight (today {d['rvol']}× 3 days ago) — "
                 f"active distribution, not a quiet drift")
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
            json={"model": model, "max_tokens": 300, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        txt = r.json()["content"][0]["text"]
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        return {"error": str(e)[:80]}


def daily_pick(market="IN", top=5, with_news=True, do_record=True):
    m = MARKETS.get(market, MARKETS["IN"])
    df, bench_1m, closes = relative_strength(m["universe"], m["bench"])
    # buy must lead the index, be in an uptrend, AND have volume confirming the move
    cand = df[(df["above_50dma"]) & (df["rs_vs_nifty"] > 0) & (df["vol_ok"])].head(top * 4)
    picks, conflicts = [], []
    for _, row in cand.iterrows():
        if len(picks) >= top:
            break
        if config.REC_SKIP_REVERSAL and row["rev_risk"]:
            continue                         # opted-in: avoid names flashing reversal warnings
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
            cat = d["catalyst"]
            if (config.REC_VETO_ON_CONFLICT and cat and cat.get("direction") == "down"
                    and (cat.get("conviction") or 0) >= config.REC_CONFLICT_CONVICTION):
                conflicts.append({"symbol": row["symbol"].replace(".NS", ""), "side": "buy",
                                  "direction": "down", "conviction": cat.get("conviction")})
                continue                     # bearish news contradicts the buy -> skip to next
        picks.append(d)
    # --- top SELL / avoid picks: the mirror of the buy side ---
    # weakest relative strength in a confirmed downtrend. No EPS/PE *filter* here
    # (a short thesis doesn't need profitability) — valuation only colours the reason.
    weak = df[(~df["above_50dma"]) & (df["rs_vs_nifty"] < 0)].sort_values("rs_vs_nifty").head(top * 4)
    sell_picks = []
    for _, row in weak.iterrows():
        if len(sell_picks) >= top:
            break
        f = fundamentals(row["symbol"])
        d = row.to_dict(); d["pe"] = f["pe"]; d["eps"] = f["eps"]
        d["reasons"] = build_sell_reasons(d, m["bench_name"])
        if with_news and not sell_picks:         # vet the catalyst for the would-be #1
            d["news"] = news_headlines(row["symbol"])
            d["catalyst"] = catalyst_score(row["symbol"], d["news"])
            cat = d["catalyst"]
            if (config.REC_VETO_ON_CONFLICT and cat and cat.get("direction") == "up"
                    and (cat.get("conviction") or 0) >= config.REC_CONFLICT_CONVICTION):
                conflicts.append({"symbol": row["symbol"].replace(".NS", ""), "side": "sell",
                                  "direction": "up", "conviction": cat.get("conviction")})
                continue                         # bullish news contradicts the short -> skip to next
        sell_picks.append(d)

    # directional options idea for the top buy (CALL) and top sell (PUT)
    if picks:
        picks[0]["option"] = OPT.suggest(picks[0]["symbol"], picks[0]["price"],
                                         closes.get(picks[0]["symbol"]), "call", market, m["cur"])
    if sell_picks:
        sell_picks[0]["option"] = OPT.suggest(sell_picks[0]["symbol"], sell_picks[0]["price"],
                                             closes.get(sell_picks[0]["symbol"]), "put", market, m["cur"])

    result = {"date": str(dt.date.today()), "market": market, "currency": m["cur"],
              "bench_name": m["bench_name"], "bench_1m_pct": bench_1m,
              "sectors": sector_rollup(df), "picks": picks, "sell_picks": sell_picks,
              "conflicts": conflicts}

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
