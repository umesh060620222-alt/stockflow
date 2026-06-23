"""Live recommend-and-grade loop.

Every ~second we poll Kite quotes for the universe, compute a fast micro-signal
(price vs VWAP + short momentum + order-book imbalance + volume picking up), and
when it fires we record a BUY recommendation. LIVE_HORIZON_S seconds later we
grade it: was the *best* exit in that window profitable after costs? The running
scorecard (accuracy) tells you whether the signal actually predicts a 30s move.

Polling via kite.quote() (one call covers the whole basket) — no WebSocket/twisted
needed. Runs in a background thread; market-closed quotes are static so nothing
fires until the market is open.
"""
from __future__ import annotations
import threading, time, datetime as dt
from collections import deque, defaultdict

import config, zerodha as Z


class LiveEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.ticks = defaultdict(lambda: deque(maxlen=180))  # (t, ltp, vwap, vol, bidq, askq)
        self.pending = []                 # open recommendations awaiting grading
        self.history = deque(maxlen=200)  # graded recommendations (newest first)
        self.score = {"total": 0, "correct": 0, "directional": 0}
        self.last_error = None
        self.started_at = None
        self.polls = 0

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.started_at = time.time()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            return True

    def stop(self):
        self.running = False

    def _keys(self):
        # quote() wants "NSE:RELIANCE" style keys
        return {f"NSE:{Z._ksym(s)}": s for s in config.UNIVERSE}

    def _loop(self):
        try:
            kc = Z.kite()
            pairs = self._keys()
            keys = list(pairs.keys())
        except Exception as e:
            self.last_error = f"start failed: {e}"
            self.running = False
            return
        while self.running:
            t0 = time.time()
            try:
                q = kc.quote(keys)
                now = time.time()
                for k, sym in pairs.items():
                    d = q.get(k)
                    if not d:
                        continue
                    ltp = d.get("last_price") or 0
                    vwap = d.get("average_price") or ltp
                    vol = d.get("volume") or 0
                    depth = d.get("depth") or {}
                    bidq = sum(x.get("quantity", 0) for x in depth.get("buy", []))
                    askq = sum(x.get("quantity", 0) for x in depth.get("sell", []))
                    self.ticks[sym].append((now, ltp, vwap, vol, bidq, askq))
                    self._signal(sym, now, ltp, vwap)
                self._grade(now)
                self.polls += 1
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
            time.sleep(max(0.2, config.LIVE_POLL_SEC - (time.time() - t0)))

    def _signal(self, sym, now, ltp, vwap):
        dq = self.ticks[sym]
        if len(dq) < config.LIVE_LOOKBACK_S + 1:
            return
        if any(p["symbol"] == sym for p in self.pending):
            return   # one open recommendation per symbol at a time
        past = dq[-config.LIVE_LOOKBACK_S - 1]
        mom = (ltp - past[1]) / past[1] if past[1] else 0
        dvol = dq[-1][3] - past[3]
        bidq, askq = dq[-1][4], dq[-1][5]
        imb = (bidq - askq) / (bidq + askq) if (bidq + askq) else 0
        if ltp > vwap and mom >= config.LIVE_MOM_MIN and imb >= config.LIVE_IMB_MIN and dvol > 0:
            with self.lock:
                self.pending.append({"symbol": sym, "side": "long", "ts": now,
                                     "entry": ltp, "best": ltp,
                                     "deadline": now + config.LIVE_HORIZON_S})

    def _grade(self, now):
        still = []
        cost = config.COST_PCT + 2 * config.SLIPPAGE_PCT
        for p in self.pending:
            dq = self.ticks[p["symbol"]]
            last = dq[-1][1] if dq else p["entry"]
            p["best"] = max(p["best"], last)
            if now < p["deadline"]:
                still.append(p)
                continue
            fav = (p["best"] - p["entry"]) / p["entry"] if p["entry"] else 0
            net_best = fav - cost
            correct = net_best >= config.LIVE_TARGET
            with self.lock:
                self.history.appendleft({
                    "symbol": p["symbol"], "side": p["side"],
                    "time": dt.datetime.fromtimestamp(p["ts"]).strftime("%H:%M:%S"),
                    "entry": round(p["entry"], 2), "best": round(p["best"], 2),
                    "best_move_pct": round(fav * 100, 3),
                    "net_best_pct": round(net_best * 100, 3),
                    "correct": correct})
                self.score["total"] += 1
                if correct:
                    self.score["correct"] += 1
                if fav > 0:
                    self.score["directional"] += 1
        self.pending = still

    def state(self):
        with self.lock:
            tot = self.score["total"]
            return {
                "running": self.running, "polls": self.polls,
                "pending": len(self.pending), "last_error": self.last_error,
                "scorecard": {
                    "total": tot, "correct": self.score["correct"],
                    "accuracy_pct": round(100 * self.score["correct"] / tot, 1) if tot else 0,
                    "directional_pct": round(100 * self.score["directional"] / tot, 1) if tot else 0,
                },
                "recent": list(self.history)[:30],
            }


ENGINE = LiveEngine()
