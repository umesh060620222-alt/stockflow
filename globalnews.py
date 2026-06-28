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


def predict_next_day(symbol: str) -> dict:
    """Predict next-day impact on an Indian stock based on overnight US market moves + news.

    Pipeline:
    1. Fetch actual US index performance today (SPY/QQQ/DJI/VIX) from yfinance
    2. Fetch overnight US financial news (last 12h) from Reuters/CNBC/MarketWatch
    3. Fetch the Indian stock's sector + business profile from yfinance
    4. Claude reasons: which news matters for this stock, direction + magnitude estimate,
       key drivers, risks, and what to watch at open
    """
    import requests as req

    symbol = symbol.strip().upper()
    now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    now_ist = now_utc + dt.timedelta(hours=5, minutes=30)

    # ── 1. US market performance ──────────────────────────────────────────────
    us_perf = {}
    try:
        import yfinance as yf
        for ticker, label in [("SPY","S&P 500"),("QQQ","Nasdaq"),("^DJI","Dow"),("^VIX","VIX")]:
            try:
                h = yf.Ticker(ticker).history(period="2d")
                if len(h) >= 2:
                    prev, last = h["Close"].iloc[-2], h["Close"].iloc[-1]
                    us_perf[label] = round((last - prev) / prev * 100, 2)
                elif len(h) == 1:
                    us_perf[label] = 0.0
            except Exception:
                pass
    except Exception:
        pass

    # ── 2. Overnight US news (last 12 hours) ─────────────────────────────────
    cutoff_min = 12 * 60
    us_news = []
    seen = set()
    for url in _FEEDS_GLOBAL:
        for item in _fetch_feed(url, n=8):
            if item["age_min"] is not None and item["age_min"] > cutoff_min:
                continue
            key = item["title"][:70]
            if key in seen:
                continue
            seen.add(key)
            us_news.append(item)
            if len(us_news) >= 15:
                break
        if len(us_news) >= 15:
            break

    # ── 3. Indian stock profile ───────────────────────────────────────────────
    company_name = symbol
    sector = ""
    industry = ""
    description = ""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol + ".NS").info
        company_name = info.get("longName") or symbol
        sector       = info.get("sector", "")
        industry     = info.get("industry", "")
        description  = (info.get("longBusinessSummary") or "")[:400]
    except Exception:
        pass

    # ── 4. Claude prediction ──────────────────────────────────────────────────
    perf_lines = "\n".join(f"  {k}: {'+' if v>=0 else ''}{v}%" for k, v in us_perf.items()) if us_perf else "  (unavailable)"
    news_lines = "\n".join(f"  {i+1}. {h['title']} ({h['age_label']})" for i, h in enumerate(us_news)) if us_news else "  (no recent headlines)"
    stock_ctx  = f"{company_name} ({symbol}.NS)\n  Sector: {sector} | Industry: {industry}\n  {description}"

    prompt = (
        "You are a macro-to-micro analyst specializing in how overnight US market developments "
        "flow into Indian equity markets the next morning.\n\n"

        "== US MARKET PERFORMANCE (today) ==\n" + perf_lines + "\n\n"

        "== OVERNIGHT US NEWS (last 12 hours) ==\n" + news_lines + "\n\n"

        "== INDIAN STOCK TO ANALYZE ==\n" + stock_ctx + "\n\n"

        "== INDIA SENSITIVITY RULES ==\n" + _INDIA_CONTEXT + "\n"

        "Analyze step by step:\n"
        "1. Which of the US news items are DIRECTLY relevant to this specific stock? Why?\n"
        "2. How does the US index move (SPY/QQQ/DJI) translate to this stock's sector in India?\n"
        "3. Are there any India-specific amplifiers or dampeners (rupee, oil, FII flows)?\n"
        "4. What is your prediction for this stock at tomorrow's Indian market open?\n\n"

        "Reply ONLY with valid JSON:\n"
        '{"direction":"up|down|flat","magnitude":"X-Y%",'
        '"confidence":0-100,'
        '"summary":"3-4 sentence plain English prediction",'
        '"key_drivers":["...","...","..."],'
        '"risks":["...","..."],'
        '"watch_at_open":"one specific thing to monitor at 9:15 AM IST"}'
    )

    key = os.getenv("ANTHROPIC_API_KEY")
    prediction = _claude_call(prompt, max_tokens=600) if key else {"error": "ANTHROPIC_API_KEY not set"}

    return {
        "updated":      now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "symbol":       symbol,
        "company":      company_name,
        "sector":       sector,
        "us_perf":      us_perf,
        "us_news":      [{"title": h["title"], "link": h["link"], "age_label": h["age_label"]}
                         for h in us_news],
        "direction":    prediction.get("direction", "flat"),
        "magnitude":    prediction.get("magnitude", ""),
        "confidence":   prediction.get("confidence", 0),
        "summary":      prediction.get("summary", ""),
        "key_drivers":  prediction.get("key_drivers", []),
        "risks":        prediction.get("risks", []),
        "watch_at_open":prediction.get("watch_at_open", ""),
        "claude_error": prediction.get("error", ""),
    }


def fetch_stock_news(symbol: str) -> dict:
    """Fetch news for a specific stock (Indian or US) + Claude price-impact analysis.

    Indian: RELIANCE, TCS, INFY, HDFC, WIPRO …
    US:     AAPL, MSFT, NVDA, TSLA …
    """
    import requests as req, xml.etree.ElementTree as ET

    symbol = symbol.strip().upper()
    now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    now_ist = now_utc + dt.timedelta(hours=5, minutes=30)

    # Detect Indian vs US — try yfinance .NS suffix
    is_indian = False
    yf_sym = symbol
    company_name = symbol
    try:
        import yfinance as yf
        t = yf.Ticker(symbol + ".NS")
        if getattr(t.fast_info, "last_price", None):
            is_indian = True
            yf_sym = symbol + ".NS"
            try:
                company_name = t.info.get("longName") or symbol
            except Exception:
                pass
    except Exception:
        pass

    if not is_indian:
        try:
            import yfinance as yf
            company_name = yf.Ticker(symbol).info.get("longName") or symbol
        except Exception:
            pass

    raw, seen = [], set()

    def _add(title, link, age_min):
        key = title[:70]
        if key in seen or not title:
            return
        seen.add(key)
        if age_min is None or age_min < 1440:
            raw.append({"title": title, "link": link, "age_min": age_min})

    # 1. yfinance news — pre-filtered to this ticker by Yahoo Finance
    try:
        import yfinance as yf
        for item in (yf.Ticker(yf_sym).news or [])[:15]:
            pub = item.get("providerPublishTime")
            age_min = round((now_utc.timestamp() - pub) / 60) if pub else None
            _add(item.get("title", ""), item.get("link", ""), age_min)
    except Exception:
        pass

    # 2. Google News RSS for company name — fills gaps
    for q in [f'"{company_name}" stock', f'"{symbol}" shares results earnings']:
        gn_url = ("https://news.google.com/rss/search?q=" +
                  req.utils.quote(q) + "&hl=en-US&gl=US&ceid=US:en")
        try:
            r = req.get(gn_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            for it in ET.fromstring(r.content).findall(".//item")[:8]:
                title = (it.findtext("title") or "").strip()
                link  = (it.findtext("link")  or "").strip()
                pub   = None
                try:
                    pub = parsedate_to_datetime(it.findtext("pubDate") or "")
                except Exception:
                    pass
                age_min = round((now_utc - pub).total_seconds() / 60) if pub else None
                _add(title, link, age_min)
        except Exception:
            pass
        if len(raw) >= 20:
            break

    raw = raw[:20]
    if not raw:
        return {"updated": now_ist.strftime("%Y-%m-%d %H:%M IST"),
                "symbol": symbol, "company": company_name,
                "overall": "neutral", "conviction": 0, "summary": "",
                "claude_error": "No news found for this symbol.", "headlines": []}

    numbered = "\n".join(f"{i+1}. {h['title']}" for i, h in enumerate(raw))
    mkt = "Indian (NSE/BSE)" if is_indian else "US"
    prompt = (
        f"You are a senior equity analyst covering {mkt} markets.\n"
        f"Company: {company_name} ({symbol}{'  — NSE-listed' if is_indian else ''})\n\n"
        "These are the latest news headlines about this stock:\n\n"
        + numbered +
        "\n\nFor EACH headline:\n"
        "- State whether it is bullish, bearish, or neutral for the stock price\n"
        "- Give one specific reason why (e.g. 'margin expansion drives EPS upgrade' or "
        "'contract loss reduces revenue visibility')\n\n"
        "Then write a 3-4 sentence overall outlook: net impact on the stock price "
        f"of {symbol} and what investors should watch next.\n\n"
        "Reply ONLY with valid JSON:\n" + _JSON_SHAPE
    )
    result = _build_result(raw, _claude_call(prompt))
    result.update({"symbol": symbol, "company": company_name, "is_indian": is_indian})
    return result
