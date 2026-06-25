"""Intraday news radar — the fast counterpart to the once-a-day recommender.

Every ~60s it pulls fresh headlines for the WATCHED symbols (the day's picks +
the index), keeps only genuinely NEW + recent items, and classifies each as an
immediate BUY / SELL / VOLATILITY signal so the UI can flash it on screen.

Directional words (beats, bags order, upgrade / probe, downgrade, recall) drive
buy vs sell; event words (results, RBI/Fed, ex-dividend, F&O ban) and conflicting
buy+sell headlines drive a VOLATILITY flag — news arriving mid-session is itself a
volatility signal. Classification is deterministic + free; Claude is an optional
conviction escalation for the strongest hits (config.NEWS_USE_CLAUDE).
"""
from __future__ import annotations
import os, re, json, time, datetime as dt
from collections import deque
from email.utils import parsedate_to_datetime
import config

# --- weighted lexicons (word-boundary matched, case-insensitive) -------------
BUY_WORDS = {
    "surge": 2, "surges": 2, "jumps": 2, "soars": 2, "rally": 2, "rallies": 2,
    "record profit": 3, "record high": 3, "all-time high": 3, "52-week high": 2,
    "beats": 2, "tops estimates": 3, "profit rises": 2, "profit jumps": 3,
    "upgrade": 3, "upgrades": 3, "raises guidance": 3, "raises target": 2,
    "bags order": 3, "wins order": 3, "wins contract": 3, "bags contract": 3,
    "order win": 3, "new order": 2, "buyback": 3, "bonus issue": 2, "dividend hike": 2,
    "stake buy": 2, "acquires": 2, "acquisition": 2, "merger": 1, "tie-up": 2,
    "partnership": 1, "approval": 2, "approved": 2, "launch": 1, "expansion": 1,
    "block deal buy": 3, "fii buying": 2, "multibagger": 2, "outperform": 2,
}
SELL_WORDS = {
    "plunge": 2, "plunges": 2, "slumps": 2, "tanks": 2, "crashes": 2, "sinks": 2,
    "tumbles": 2, "falls": 1, "drops": 1, "skids": 2, "record low": 3, "52-week low": 2,
    "misses": 2, "misses estimates": 3, "profit falls": 2, "profit drops": 2,
    "loss widens": 3, "posts loss": 3, "swings to loss": 3, "downgrade": 3,
    "downgrades": 3, "cuts guidance": 3, "cuts target": 2, "guidance cut": 3,
    "fraud": 3, "probe": 3, "investigation": 2, "raid": 3, "searches": 2, "ed summons": 3,
    "sebi": 1, "penalty": 2, "fine": 1, "ban": 2, "recall": 3, "lawsuit": 2, "default": 3,
    "resigns": 2, "steps down": 2, "ceo exit": 2, "stake sale": 2, "block deal sell": 3,
    "fii selling": 2, "downgraded": 3, "underperform": 2, "warning": 2, "halt": 2,
}
VOL_WORDS = {
    "results": 2, "q1 results": 3, "q2 results": 3, "q3 results": 3, "q4 results": 3,
    "earnings": 2, "board meeting": 2, "agm": 1, "ex-dividend": 2, "ex-date": 2,
    "stock split": 2, "f&o ban": 3, "fno ban": 3, "circuit": 3, "volatile": 2,
    "rbi": 2, "fed": 2, "rate decision": 3, "rate hike": 2, "rate cut": 2, "inflation": 1,
    "budget": 2, "tariff": 2, "tariffs": 2, "gst": 1, "election": 1, "guidance": 1,
    "buzzing": 2, "in focus": 2, "to watch": 1, "alert": 1, "surge or": 1,
}


def _compile(words):
    return [(re.compile(r"\b" + re.escape(w) + r"\b", re.I), w, wt) for w, wt in words.items()]


_BUY, _SELL, _VOL = _compile(BUY_WORDS), _compile(SELL_WORDS), _compile(VOL_WORDS)


def _hits(text, compiled):
    out, score = [], 0
    for rx, w, wt in compiled:
        if rx.search(text):
            out.append(w); score += wt
    return out, score


def classify(title: str) -> dict:
    """Deterministic signal from one headline.
    Returns {signal: buy|sell|volatile|neutral, score, conviction, matched:[...]}."""
    t = title or ""
    buy_hits, buy = _hits(t, _BUY)
    sell_hits, sell = _hits(t, _SELL)
    vol_hits, vol = _hits(t, _VOL)
    net = buy - sell

    if buy and sell:                         # conflicting drivers -> expect a swing
        signal, score, matched = "volatile", buy + sell + vol, buy_hits + sell_hits + vol_hits
    elif net >= 2:
        signal, score, matched = "buy", buy, buy_hits
    elif net <= -2:
        signal, score, matched = "sell", sell, sell_hits
    elif vol >= 2 or (net != 0 and vol):     # event-driven -> volatility
        signal, score, matched = "volatile", vol + abs(net), vol_hits + buy_hits + sell_hits
    elif net > 0:
        signal, score, matched = "buy", buy, buy_hits
    elif net < 0:
        signal, score, matched = "sell", sell, sell_hits
    elif vol:
        signal, score, matched = "volatile", vol, vol_hits
    else:
        return {"signal": "neutral", "score": 0, "conviction": 0, "matched": []}
    return {"signal": signal, "score": score,
            "conviction": min(95, 35 + score * 15), "matched": matched}


def aggregate_signal(headlines):
    """Roll a stock's recent classified headlines into one signal + conviction."""
    if not headlines:
        return "neutral", 0
    buy = sum(h["score"] for h in headlines if h["signal"] == "buy")
    sell = sum(h["score"] for h in headlines if h["signal"] == "sell")
    vol = sum(h["score"] for h in headlines if h["signal"] == "volatile")
    net = buy - sell
    if buy and sell and abs(net) < 2:
        sig, sc = "volatile", buy + sell + vol      # mixed drivers -> expect a swing
    elif net >= 2:
        sig, sc = "buy", buy
    elif net <= -2:
        sig, sc = "sell", sell
    elif vol >= 2:
        sig, sc = "volatile", vol
    elif net > 0:
        sig, sc = "buy", buy
    elif net < 0:
        sig, sc = "sell", sell
    elif vol:
        sig, sc = "volatile", vol
    else:
        sig, sc = "neutral", 0
    return sig, min(95, 35 + sc * 10)


def _fetch_items(query: str, n=8):
    """Google News RSS -> [{title, guid, pub(datetime|None)}], newest first."""
    import requests, xml.etree.ElementTree as ET
    url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(query) +
           "&hl=en-IN&gl=IN&ceid=IN:en")
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        items = []
        for it in ET.fromstring(r.content).findall(".//item")[:n]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link or title).strip()
            pub = None
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:
                pass
            if title:
                items.append({"title": title, "guid": guid, "pub": pub, "link": link})
        return items
    except Exception:
        return []


def _claude_conviction(symbol, title):
    """Optional: tighten the directional read with one cheap Claude call."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import requests
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    prompt = ("One stock headline. Reply ONLY JSON "
              '{"direction":"up|down|neutral","conviction":0-100}. '
              f"Stock {symbol}: {title}")
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 60,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=20)
        m = re.search(r"\{.*\}", r.json()["content"][0]["text"], re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


class NewsRadar:
    """Per-market rolling feed of new, classified, time-stamped headlines."""

    def __init__(self, market="IN"):
        self.market = market
        self.seen = set()                                  # guids already emitted as flashes
        self.alerts = deque(maxlen=getattr(config, "NEWS_FEED_KEEP", 40))
        self.stocks = {}                                   # symbol -> per-stock card (always populated)
        self.updated = None
        self.suffix = ".NS" if market == "IN" else ""

    def _now_ist(self):
        return dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)

    def poll(self, symbols, bench_query=None):
        """Fetch + classify recent headlines per watched symbol. Builds a per-stock
        card (ALWAYS populated, so names + supporting news are visible) and emits
        flash alerts for genuinely NEW directional / volatility headlines."""
        max_age = getattr(config, "NEWS_MAX_AGE_MIN", 180)
        per = getattr(config, "NEWS_HEADLINES_PER_STOCK", 6)
        use_claude = getattr(config, "NEWS_USE_CLAUDE", False)
        now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        fresh, cards = [], {}
        queries = [(s, s.replace(self.suffix, "") + " stock") for s in symbols]
        if bench_query:
            queries.append(("__MKT__", bench_query))

        for sym, q in queries:
            name = "MARKET" if sym == "__MKT__" else sym.replace(self.suffix, "")
            headlines = []
            for it in _fetch_items(q, n=max(per, 8)):
                c = classify(it["title"])
                age_min = None
                if it["pub"] is not None:
                    age_min = round((now_utc - it["pub"]).total_seconds() / 60)
                headlines.append({
                    "title": it["title"], "signal": c["signal"], "score": c["score"],
                    "matched": c["matched"][:4], "conviction": c["conviction"],
                    "link": it.get("link", ""), "age_min": age_min,
                    "ts": self._now_ist().strftime("%H:%M"),
                })
                # flash only NEW + recent + non-neutral headlines
                key = sym + "|" + it["guid"]
                recent = age_min is None or (-5 <= age_min <= max_age)
                if key not in self.seen and recent and c["signal"] != "neutral":
                    self.seen.add(key)
                    alert = {"id": key, "symbol": name, "signal": c["signal"],
                             "conviction": c["conviction"], "matched": c["matched"][:4],
                             "title": it["title"], "link": it.get("link", ""),
                             "ts": self._now_ist().strftime("%H:%M"),
                             "epoch": int((it["pub"] or now_utc).timestamp())}
                    if use_claude and c["signal"] in ("buy", "sell") and c["score"] >= 3:
                        cl = _claude_conviction(name, it["title"])
                        if cl and cl.get("conviction") is not None:
                            alert["conviction"] = int(cl["conviction"]); alert["claude"] = cl.get("direction")
                    fresh.append(alert)

            recent_hl = headlines[:per]
            sig, conv = aggregate_signal(recent_hl)
            cards[name] = {"symbol": name, "signal": sig, "conviction": conv,
                           "n": len(recent_hl), "headlines": recent_hl}

        if len(self.seen) > 4000:
            self.seen = set(list(self.seen)[-2000:])
        fresh.sort(key=lambda a: a["epoch"])
        for a in fresh:
            self.alerts.append(a)
        # directional names first (by conviction), then volatile, then neutral
        rank = {"buy": 0, "sell": 0, "volatile": 1, "neutral": 2}
        self.stocks = dict(sorted(cards.items(),
                                  key=lambda kv: (rank.get(kv[1]["signal"], 3), -kv[1]["conviction"])))
        self.updated = self._now_ist().strftime("%H:%M:%S")
        return fresh

    def feed(self):
        return {"market": self.market, "updated": self.updated,
                "stocks": list(self.stocks.values()),
                "alerts": list(self.alerts)[::-1]}     # newest first


RADARS = {}


def get_radar(market="IN"):
    return RADARS.setdefault(market, NewsRadar(market))
