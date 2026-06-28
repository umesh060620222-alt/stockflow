"""Global macro + India market news from real financial news RSS channels.

fetch_global_news() — pulls from Reuters, CNBC, Yahoo Finance, MarketWatch, Investing.com
fetch_india_news()  — pulls from ET Markets, Moneycontrol, Business Standard, LiveMint

Both send headlines to Claude, which annotates each one with market impact and writes
an overall summary. Returns {updated, overall, conviction, summary, claude_error, headlines}.
"""
from __future__ import annotations
import re, json, os, datetime as dt
from email.utils import parsedate_to_datetime

# Real financial news RSS channels — market-curated, no keyword hacks
_FEEDS_GLOBAL = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "https://finance.yahoo.com/rss/topfinstories",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.investing.com/rss/news.rss",
]

_FEEDS_INDIA = [
    "https://economictimes.indiatimes.com/markets/rss.cms",
    "https://economictimes.indiatimes.com/news/economy/rss.cms",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.business-standard.com/rss/economy-policy-102.rss",
    "https://www.livemint.com/rss/markets",
]

_INDIA_CONTEXT = (
    "Key India market sensitivities you must use when reasoning:\n"
    "- India imports ~85% of crude → oil spike = rupee falls + inflation + trade deficit = Nifty bearish\n"
    "- Iran is a top crude supplier → Iran war/sanctions = direct import cost shock for India\n"
    "- Fed rate hikes → FII outflows from Indian equities to US bonds → Nifty falls\n"
    "- Strong USD → weak rupee → imported inflation → RBI tightening pressure → bearish\n"
    "- IT sector (20%+ of Nifty) earns USD → strong dollar = bullish for IT stocks\n"
    "- FMCG / auto / paints / aviation are crude-linked → oil spike hurts their margins\n"
    "- China border tension → supply chain risk for Indian manufacturing\n"
    "- FII flows are the single biggest short-term driver of Nifty direction\n"
    "- Gold uncertainty spike → MCX gold rally, jewellery stocks move\n"
)

_JSON_SHAPE = (
    '{"overall":"bullish|bearish|mixed|neutral","conviction":0-100,'
    '"summary":"3-4 sentence impact summary",'
    '"headlines":[{"idx":1,"impact":"bullish|bearish|neutral","reason":"one line"}]}'
)


def _fetch_feed(url: str, n: int = 8) -> list[dict]:
    import requests, xml.etree.ElementTree as ET
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


def _collect_from_feeds(feeds: list[str], limit: int = 20) -> list[dict]:
    seen, raw = set(), []
    for url in feeds:
        for item in _fetch_feed(url, n=6):
            key = item["title"][:70]
            if key in seen:
                continue
            seen.add(key)
            raw.append(item)
            if len(raw) >= limit:
                return raw
    return raw


def _claude_call(prompt: str, max_tokens: int = 1400) -> dict:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return {"error": "ANTHROPIC_API_KEY not set"}
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
        resp = r.json()
        if "error" in resp:
            return {"error": resp["error"].get("message", str(resp["error"]))}
        text = resp["content"][0]["text"]
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {"error": f"No JSON in response: {text[:200]}"}
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}"}
    except Exception as e:
        return {"error": str(e)}


def _build_result(raw: list, annotation: dict) -> dict:
    now_ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
    lkp = {}
    if annotation.get("headlines"):
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
        "updated":      now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "overall":      annotation.get("overall", "neutral"),
        "conviction":   annotation.get("conviction", 0),
        "summary":      annotation.get("summary", ""),
        "claude_error": annotation.get("error", ""),
        "headlines":    headlines,
    }


def fetch_global_news() -> dict:
    raw = _collect_from_feeds(_FEEDS_GLOBAL)
    if not raw:
        return {"updated": "", "overall": "neutral", "conviction": 0,
                "summary": "", "claude_error": "No headlines fetched.", "headlines": []}
    numbered = "\n".join(f"{i+1}. {h['title']}" for i, h in enumerate(raw))
    prompt = (
        "You are a senior macro strategist. These are the latest headlines from major "
        "financial news channels (Reuters, CNBC, Yahoo Finance, MarketWatch).\n\n"
        + numbered +
        "\n\nFor EACH headline, give a one-line reason how it affects US equity markets "
        "(S&P 500, Nasdaq, Dow). Then write an overall 3-4 sentence market impact summary.\n\n"
        "Reply ONLY with valid JSON:\n" + _JSON_SHAPE
    )
    return _build_result(raw, _claude_call(prompt))


def fetch_india_news() -> dict:
    raw = _collect_from_feeds(_FEEDS_INDIA)
    if not raw:
        return {"updated": "", "overall": "neutral", "conviction": 0,
                "summary": "", "claude_error": "No headlines fetched.", "headlines": []}
    numbered = "\n".join(f"{i+1}. {h['title']}" for i, h in enumerate(raw))
    prompt = (
        "You are an expert on Indian equity markets (Nifty 50, Sensex, NSE).\n\n"
        + _INDIA_CONTEXT + "\n"
        "These are the latest headlines from Indian financial news channels "
        "(ET Markets, Moneycontrol, Business Standard, LiveMint):\n\n"
        + numbered +
        "\n\nFor EACH headline, give a one-line reason how it specifically affects "
        "Nifty/Sensex or key Indian sectors (IT, FMCG, auto, banks, oil & gas). "
        "Then write an overall 3-4 sentence outlook for Indian equities.\n\n"
        "Reply ONLY with valid JSON:\n" + _JSON_SHAPE
    )
    return _build_result(raw, _claude_call(prompt))
