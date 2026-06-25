"""Local web UI to run + tune the algo. Zero heavy deps (stdlib http.server).

    python app.py        # then open http://127.0.0.1:8000

POST /api/run  applies the posted params to config at runtime, runs the replay
on the configured data source, and returns {summary, trades, sessions}.
"""
from __future__ import annotations
import json, os, traceback, threading, time, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config, data as D, strategy as S, engine as E, zerodha as Z
import recommend as REC
import newsflash as NF
from live import ENGINE as LIVE

_REC_CACHE = {}   # market -> {"date": ..., "data": ...}

HERE = os.path.dirname(__file__)

# params the UI may override -> (config attr, cast)
PARAMS = {
    "source": ("SOURCE", str), "interval": ("INTERVAL", str), "period": ("PERIOD", str),
    "mode": ("MODE", str), "deviation_pct": ("DEVIATION_PCT", float),
    "vol_mult": ("VOL_MULT", float), "mom_lookback": ("MOM_LOOKBACK", int),
    "target_pct": ("TARGET_PCT", float), "stop_pct": ("STOP_PCT", float),
    "time_stop_min": ("TIME_STOP_MIN", int), "max_positions": ("MAX_POSITIONS", int),
    "cost_pct": ("COST_PCT", float), "slippage_pct": ("SLIPPAGE_PCT", float),
}


def _jsonable(o):
    """Coerce numpy/pandas scalars that json doesn't natively handle."""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def dumps(o):
    return json.dumps(o, default=_jsonable)


def defaults():
    return {k: getattr(config, attr) for k, (attr, _) in PARAMS.items()}


def run_algo(overrides: dict) -> dict:
    for k, (attr, cast) in PARAMS.items():
        if k in overrides and overrides[k] not in (None, ""):
            setattr(config, attr, cast(overrides[k]))

    raw = D.fetch()
    bench = raw.get(config.BENCHMARK)
    bench_ret = None
    if bench is not None and not bench.empty:
        bench_ret = bench.groupby("date")["close"].transform(lambda s: s / s.iloc[0] - 1.0)

    prepared = {}
    for sym, df in raw.items():
        if sym == config.BENCHMARK or df is None or df.empty:
            continue
        prepared[sym] = S.add_indicators(df, bench_ret)
    if not prepared:
        return {"error": "No data fetched. Check the data source / login."}

    sessions = sorted({str(d) for df in prepared.values() for d in df["date"].unique()})
    result = E.run(prepared)
    # make trades JSON-safe (timestamps -> strings)
    trades = []
    for t in result["trades"]:
        t = dict(t)
        t["entry_ts"] = t["entry_ts"].strftime("%Y-%m-%d %H:%M")
        t["exit_ts"] = t["exit_ts"].strftime("%H:%M")
        trades.append(t)
    return {"summary": result["summary"], "trades": trades,
            "sessions": sessions, "n_symbols": len(prepared), "params": defaults()}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0]          # ignore query string when routing
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/defaults":
            return self._send(200, dumps(defaults()))
        if path == "/api/auth/status":
            return self._send(200, dumps({"connected": Z.auth_status(), "source": config.SOURCE}))
        if path == "/api/live/state":
            return self._send(200, dumps(LIVE.state()))
        if path == "/api/recommend":
            import datetime as _dt
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            market = q.get("market", ["IN"])[0].upper()
            if market not in ("IN", "US"):
                market = "IN"
            today = str(_dt.date.today())
            c = _REC_CACHE.get(market)
            if not c or c["date"] != today or "refresh" in q:
                try:
                    _REC_CACHE[market] = {"date": today, "data": REC.daily_pick(market=market)}
                except Exception as e:
                    return self._send(200, dumps({"error": f"{type(e).__name__}: {e}"}))
            return self._send(200, dumps(_REC_CACHE[market]["data"]))
        if path == "/api/newsflash":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            market = q.get("market", ["IN"])[0].upper()
            if market not in ("IN", "US"):
                market = "IN"
            return self._send(200, dumps(NF.get_radar(market).feed()))
        if path == "/api/auth/url":
            try:
                return self._send(200, dumps({"url": Z.login_url()}))
            except Exception as e:
                return self._send(200, dumps({"error": str(e)}))
        self._send(404, dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}") if n else {}
        if path == "/api/auth/token":
            try:
                user = Z.exchange_token(body.get("request_token", ""))
                return self._send(200, dumps({"connected": True, "user": user}))
            except Exception as e:
                return self._send(200, dumps({"error": f"{type(e).__name__}: {e}"}))
        if path == "/api/live/start":
            if not Z.auth_status():
                return self._send(200, dumps({"error": "connect Zerodha first"}))
            LIVE.start()
            return self._send(200, dumps(LIVE.state()))
        if path == "/api/live/stop":
            LIVE.stop()
            return self._send(200, dumps(LIVE.state()))
        if path != "/api/run":
            return self._send(404, dumps({"error": "not found"}))
        try:
            out = run_algo(body)
            self._send(200, dumps(out))
        except Exception as e:
            traceback.print_exc()
            self._send(500, dumps({"error": f"{type(e).__name__}: {e}"}))

    def log_message(self, *a):
        pass


def _auto_record():
    """Hands-off: once a day after ~16:00 IST, compute + record the daily pick so the
    track record builds even on days nobody opens the app."""
    while True:
        try:
            ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            today = str(ist.date())
            if ist.hour >= 16:
                for mk in ("IN", "US"):
                    c = _REC_CACHE.get(mk)
                    if not c or c["date"] != today:
                        _REC_CACHE[mk] = {"date": today, "data": REC.daily_pick(market=mk)}
                        print(f"[auto-record] {mk} pick saved for {today}", flush=True)
        except Exception as e:
            print(f"[auto-record] {e}", flush=True)
        time.sleep(1800)   # check every 30 min


def _news_watchlist(market):
    """Symbols the radar watches: today's cached picks, else the top of the universe."""
    c = _REC_CACHE.get(market)
    picks = (c or {}).get("data", {}).get("picks") if c else None
    if picks:
        return [p["symbol"] for p in picks]
    uni = REC.MARKETS.get(market, REC.MARKETS["IN"])["universe"]
    return uni[:config.NEWS_WATCH_FALLBACK]


def _news_radar_loop():
    """Hands-off: every ~60s pull fresh headlines for the watched names in each
    market and classify them into buy/sell/volatility flashes for the UI."""
    while True:
        try:
            for mk in ("IN", "US"):
                m = REC.MARKETS.get(mk, REC.MARKETS["IN"])
                NF.get_radar(mk).poll(_news_watchlist(mk), bench_query=m["bench_name"])
        except Exception as e:
            print(f"[news-radar] {e}", flush=True)
        time.sleep(max(20, config.NEWS_POLL_SEC))


threading.Thread(target=_auto_record, daemon=True).start()
threading.Thread(target=_news_radar_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")   # 0.0.0.0 so Railway can route to it
    print(f"stockflow UI on {host}:{port}   (source={config.SOURCE})")
    ThreadingHTTPServer((host, port), H).serve_forever()
