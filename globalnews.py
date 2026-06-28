"""Global macro + India macro news — events that move US and Indian equity markets.

fetch_global_news() — top 20 global headlines → Claude annotates each + overall summary
fetch_india_news()  — top 20 India-focused headlines → Claude explains Nifty/Sensex impact
                       using built-in India sensitivity context (oil, FII, rupee, RBI…)

Both functions return the same shape:
  {updated, overall, conviction, summary, headlines: [{title, link, age_label, impact, reason}]}
"""
from __future__ import annotations
import re, json, os, datetime as dt
from email.utils import parsedate_to_datetime

_QUERIES_GLOBAL = [
    "Iran Israel war attack strike",
    "Russia Ukraine war conflict",
    "China Taiwan US tensions",
    "North Korea nuclear missile",
    "Middle East conflict oil",
    "Federal Reserve interest rates inflation",
    "US economy recession GDP jobs",
    "US China trade tariffs sanctions",
    "oil price OPEC crude supply",
    "global markets stocks bonds dollar",
]

_QUERIES_INDIA = [
    "India Nifty Sensex stock market",
    "RBI Reserve Bank India rates inflation",
    "India rupee dollar exchange rate",
    "India FII DII foreign institutional investors",
    "India crude oil import OPEC",
    "India Iran oil trade",
    "India China border economy",
    "India US trade exports",
    "India GDP growth employment",
    "India budget fiscal deficit bonds",
]

_INDIA_CONTEXT = (
    "Key India market sensitivities you must use when reasoning:\n"
    "- India imports ~85% of crude → oil spike = rupee falls + inflation + trade deficit = Nifty bearish\n"
    "- Iran is a top crude supplier to India → Iran war/sanctions = direct import cost shock\n"
    "- Fed rate hikes → FII outflows from Indian equities to US bonds → Nifty falls\n"
    "- Strong USD → weak rupee → imported inflation → RBI forced to hike → bearish for rate-sensitive sectors\n"
    "- IT sector (20%+ of Nifty) earns USD revenue → strong dollar = bullish for IT\n"
    "- FMCG / auto / paints / aviation are crude-linked → oil spike hurts margins\n"
    "- China border tension → supply chain risk for Indian manufacturing\n"
    "- FII flows are the single biggest short-term driver of Nifty direction\n"
    "- Gold rises on global uncertainty → MCX gold rally, jewellery stocks move\n"
)


def _fetch_rss(query: str, n: int = 5):
    import requests, xml.etree.ElementTree as ET
    url = ("https://news.google.com/rss/search?q=" +
           requests.utils.quote(query) + "&hl=en-IN&gl=IN&ceid=IN:en")
    now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        out = []
        for it in ET.fromstring(r.content).findall(".//item")[:n]:
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link")  or "").strip()
            pub   = None
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            except Exception:
                pass
            age_min = round((now_utc - pub).total_seconds() / 60) if pub else None
            if title and (age_min is None or age_min < 1440):
                out.append({"title": title, "link": link, "age_min": age_min})
        return out
    except Exception:
        return []


def _age_label(age_min):
    if age_min is None:
        return ""
    if age_min < 60:
        return f"{age_min}m ago"
    return f"{age_min // 60}h ago"


def _collect(queries, limit=20):
    seen, raw = set(), []
    for q in queries:
        for item in _fetch_rss(q, n=4):
            key = item["title"][:70]
            if key in seen:
                continue
            seen.add(key)
            raw.append(item)
            if len(raw) >= limit:
                return raw
    return raw


def _claude_call(prompt: str, max_tokens: int = 1400) -> dict | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import requests
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        text = r.json()["content"][0]["text"]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def _build_result(raw: list, annotation: dict | None) -> dict:
    now_ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
    lkp = {}
    if annotation and annotation.get("headlines"):
        for h in annotation["headlines"]:
            idx = int(h.get("idx", 0)) - 1
            if 0 <= idx < len(raw):
                lkp[idx] = {"impact": h.get("impact", "neutral"),
                             "reason": h.get("reason", "")}
    headlines = []
    for i, item in enumerate(raw):
        ann = lkp.get(i, {})
        headlines.append({
            "title":     item["title"],
            "link":      item["link"],
            "age_label": _age_label(item["age_min"]),
            "impact":    ann.get("impact", "neutral"),
            "reason":    ann.get("reason", ""),
        })
    return {
        "updated":    now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "overall":    (annotation or {}).get("overall", "neutral"),
        "conviction": (annotation or {}).get("conviction", 0),
        "summary":    (annotation or {}).get("summary", ""),
        "headlines":  headlines,
    }


_JSON_SHAPE = (
    '{"overall":"bullish|bearish|mixed|neutral","conviction":0-100,'
    '"summary":"3-4 sentence impact summary",'
    '"headlines":[{"idx":1,"impact":"bullish|bearish|neutral","reason":"one line"}]}'
)


def fetch_global_news() -> dict:
    raw = _collect(_QUERIES_GLOBAL)
    if not raw:
        return {"updated": "", "overall": "neutral", "conviction": 0,
                "summary": "No headlines found.", "headlines": []}
    numbered = "\n".join(f"{i+1}. {h['title']}" for i, h in enumerate(raw))
    prompt = (
        "You are a senior macro strategist. These are today's top global news headlines.\n\n"
        + numbered +
        "\n\nFor EACH headline, give a one-line reason how it affects US equity markets "
        "(S&P 500, Nasdaq, Dow). Then write an overall 3-4 sentence market impact summary.\n\n"
        "Reply ONLY with valid JSON:\n" + _JSON_SHAPE
    )
    return _build_result(raw, _claude_call(prompt))


def fetch_india_news() -> dict:
    raw = _collect(_QUERIES_INDIA)
    if not raw:
        return {"updated": "", "overall": "neutral", "conviction": 0,
                "summary": "No headlines found.", "headlines": []}
    numbered = "\n".join(f"{i+1}. {h['title']}" for i, h in enumerate(raw))
    prompt = (
        "You are an expert on Indian equity markets (Nifty 50, Sensex, NSE).\n\n"
        + _INDIA_CONTEXT + "\n"
        "These are today's India-focused macro news headlines:\n\n"
        + numbered +
        "\n\nFor EACH headline, give a one-line reason how it specifically affects "
        "Nifty/Sensex or key Indian sectors (IT, FMCG, auto, banks, oil & gas). "
        "Then write an overall 3-4 sentence outlook for Indian equities today.\n\n"
        "Reply ONLY with valid JSON:\n" + _JSON_SHAPE
    )
    return _build_result(raw, _claude_call(prompt))
