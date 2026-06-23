"""Local web UI to run + tune the algo. Zero heavy deps (stdlib http.server).

    python app.py        # then open http://127.0.0.1:8000

POST /api/run  applies the posted params to config at runtime, runs the replay
on the configured data source, and returns {summary, trades, sessions}.
"""
from __future__ import annotations
import json, os, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config, data as D, strategy as S, engine as E

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
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/defaults":
            return self._send(200, dumps(defaults()))
        self._send(404, dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/run":
            return self._send(404, dumps({"error": "not found"}))
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or "{}")
            out = run_algo(body)
            self._send(200, dumps(out))
        except Exception as e:
            traceback.print_exc()
            self._send(500, dumps({"error": f"{type(e).__name__}: {e}"}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")   # 0.0.0.0 so Railway can route to it
    print(f"stockflow UI on {host}:{port}   (source={config.SOURCE})")
    ThreadingHTTPServer((host, port), H).serve_forever()
