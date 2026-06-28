"""Global macro news — geopolitical + economic events that move US equities.

fetch_global_news():
  1. Pulls up to 20 unique headlines from Google News RSS across broad macro themes.
  2. Sends all headlines to Claude in one call.
  3. Claude annotates each headline (bullish / bearish / neutral + one-line reason)
     and writes an overall market impact summary.
  4. Returns headlines + Claude analysis ready for the UI.

If no ANTHROPIC_API_KEY, returns headlines with no LLM annotation.
"""
from __future__ import annotations
import re, json, os, datetime as dt
from email.utils import parsedate_to_datetime

# Broad queries — geo-political, macro-economic, no stock symbols
_QUERIES = [
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


def _fetch_rss(query: str, n: int = 5):
    import requests, xml.etree.ElementTree as ET
    url = ("https://news.google.com/rss/search?q=" +
           requests.utils.quote(query) + "&hl=en-US&gl=US&ceid=US:en")
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


def _claude_annotate(headlines: list[dict]) -> dict | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import requests
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    numbered = "\n".join(f"{i+1}. {h['title']}" for i, h in enumerate(headlines))
    prompt = (
        "You are a senior macro strategist. Below are today's top global news headlines "
        "that may affect US equity markets (S&P 500, Nasdaq, Dow Jones).\n\n"
        + numbered +
        "\n\nFor EACH headline give a one-line reason how it affects US stocks. "
        "Then write an overall 3-4 sentence market impact summary.\n\n"
        "Reply ONLY with valid JSON in this exact shape:\n"
        '{"overall":"bullish|bearish|mixed|neutral","conviction":0-100,'
        '"summary":"3-4 sentence overall market impact",'
        '"headlines":[{"idx":1,"impact":"bullish|bearish|neutral","reason":"one line"}]}'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 1200, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        text = r.json()["content"][0]["text"]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def fetch_global_news() -> dict:
    now_ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
    seen, raw = set(), []

    for q in _QUERIES:
        for item in _fetch_rss(q, n=4):
            key = item["title"][:70]
            if key in seen:
                continue
            seen.add(key)
            raw.append(item)
            if len(raw) >= 20:
                break
        if len(raw) >= 20:
            break

    # LLM annotation
    annotation = _claude_annotate(raw) if raw else None

    # Merge LLM per-headline impacts back
    lkp = {}
    if annotation and annotation.get("headlines"):
        for h in annotation["headlines"]:
            idx = h.get("idx", 0) - 1
            if 0 <= idx < len(raw):
                lkp[idx] = {"impact": h.get("impact", "neutral"),
                             "reason": h.get("reason", "")}

    headlines = []
    for i, item in enumerate(raw):
        ann = lkp.get(i, {})
        headlines.append({
            "title":    item["title"],
            "link":     item["link"],
            "age_label": _age_label(item["age_min"]),
            "impact":   ann.get("impact", "neutral"),
            "reason":   ann.get("reason", ""),
        })

    return {
        "updated":   now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "overall":   (annotation or {}).get("overall", "neutral"),
        "conviction":(annotation or {}).get("conviction", 0),
        "summary":   (annotation or {}).get("summary", ""),
        "headlines": headlines,
    }
