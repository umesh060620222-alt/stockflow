"""Auto trader — picks top scanner stock at 9:07:30 IST, buys 1 share at market open.

Timeline:
  9:00        Scanner collecting ticks (connect WS manually or via /api/autotrader/start)
  9:07:30     Lock in top pick (score >= 5, ratio >= 1.2, price up)
  9:15:00     Place market BUY for 1 share
  on fill     Place SL-M at 5% below avg fill price
"""
from __future__ import annotations
import datetime, threading, time, json, logging, os

log = logging.getLogger("autotrader")

SL_PCT    = 0.05
PAPER     = os.getenv("AT_PAPER", "0") == "1"   # set AT_PAPER=1 to log instead of place real orders

class AutoTrader:
    def __init__(self):
        self.pick       = None   # {"symbol", "score", "ratio", "chgPct"}
        self.state      = "idle" # idle | watching | locked | ordered | done | error
        self.order_id   = None
        self.sl_order_id= None
        self.fill_price = None
        self.log        = []
        self._thread    = None
        self._stop      = False

    def _ist(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

    def _log(self, msg):
        ts = self._ist().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.log.append(entry)
        log.info(entry)

    def start(self, scanner_data_fn):
        """Start the auto-trading loop. scanner_data_fn() returns current scData dict."""
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._loop, args=(scanner_data_fn,), daemon=True)
        self._thread.start()
        self.state = "watching"
        self._log("AutoTrader started — watching scanner until 9:07:30")

    def stop(self):
        self._stop = True
        self.state = "idle"
        self._log("AutoTrader stopped.")

    def _loop(self, scanner_data_fn):
        try:
            self._watch_and_pick(scanner_data_fn)
            if self._stop or not self.pick:
                return
            self._wait_for_open()
            if self._stop:
                return
            self._place_order()
        except Exception as e:
            self.state = "error"
            self._log(f"ERROR: {e}")

    def _watch_and_pick(self, scanner_data_fn):
        """Wait until 9:07:30 then pick top scorer."""
        self._log("Watching scanner scores…")
        while not self._stop:
            ist = self._ist()
            h, m, s = ist.hour, ist.minute, ist.second
            # lock in pick at 9:08:00 (auction closed, final scores)
            if h == 9 and m >= 8:
                break
            # before 9:00 — wait
            if h < 9:
                time.sleep(10)
                continue
            time.sleep(1)

        if self._stop:
            return

        # score all stocks and pick top
        sc = scanner_data_fn()
        best = None
        for token, d in sc.items():
            if not d.get("ticks") or len(d["ticks"]) < 3:
                continue
            s = _score(d)
            chgPct = d["ticks"][-1].get("chgPct", 0)
            ratio  = d["ticks"][-1].get("ratio", 0)
            if s["total"] >= MIN_SCORE and ratio >= MIN_RATIO and chgPct > 0:
                if best is None or s["total"] > best["score"]:
                    best = {"symbol": d["sym"], "score": s["total"],
                            "ratio": round(ratio, 2), "chgPct": round(chgPct, 2)}

        if best:
            self.pick = best
            self.state = "locked"
            self._log(f"PICK LOCKED: {best['symbol']} score={best['score']}/7 "
                      f"ratio={best['ratio']} chg={best['chgPct']:+.2f}%")
        else:
            self.state = "done"
            self._log("No qualifying pick found (score<5 or ratio<1.2 or price not up). No trade.")

    def _wait_for_open(self, delay=None):
        if delay is not None:
            self._log(f"[TEST] Firing in {delay}s…")
            time.sleep(delay)
            return
        self._log(f"Pick locked: {self.pick['symbol']} — waiting for 9:15:00…")
        while not self._stop:
            ist = self._ist()
            if ist.hour == 9 and ist.minute >= 15:
                break
            if ist.hour > 9:
                break
            time.sleep(1)

    def _atm_strike(self, ltp):
        for threshold, iv in [(100,2.5),(250,5),(500,10),(1000,25),(2500,50)]:
            if ltp <= threshold:
                return round(ltp / iv) * iv
        return round(ltp / 100) * 100

    def _get_option(self, kc, sym, ltp, side):
        """Return nearest-expiry ATM option instrument from Kite NFO."""
        strike   = self._atm_strike(ltp)
        opt_type = "CE" if side == "BUY" else "PE"
        today    = datetime.date.today()
        rows     = kc.instruments("NFO")
        matches  = [r for r in rows
                    if r.get("name") == sym
                    and float(r.get("strike") or 0) == strike
                    and r.get("instrument_type") == opt_type
                    and r.get("expiry") and r["expiry"] >= today]
        if not matches:
            return None
        matches.sort(key=lambda r: r["expiry"])
        return matches[0]

    def _place_order(self):
        sym  = self.pick["symbol"]
        ltp  = self.pick.get("ltp", 0)
        side = self.pick.get("side", "BUY").upper()

        if PAPER:
            strike = self._atm_strike(ltp)
            opt_type = "CE" if side == "BUY" else "PE"
            self._log(f"[PAPER] BUY 1 lot {sym} {strike} {opt_type} @ market")
            self.order_id  = "PAPER-001"
            self.state     = "ordered"
            time.sleep(2)
            import random
            self.fill_price = round(ltp * 0.015 * (1 + random.uniform(-0.05, 0.05)), 2)
            sl_trigger = round(self.fill_price * 0.50, 2)  # SL at 50% of premium
            self.sl_order_id = "PAPER-002"
            self.state = "done"
            self._log(f"[PAPER] FILLED premium ₹{self.fill_price} · SL at ₹{sl_trigger}")
            return

        import zerodha as Z
        kc = Z.kite()
        opt = self._get_option(kc, sym, ltp, side)
        if not opt:
            self.state = "error"
            self._log(f"No ATM option found for {sym} @ ₹{ltp}")
            return

        tradingsymbol = opt["tradingsymbol"]
        lot_size      = int(opt.get("lot_size") or 1)
        self._log(f"Placing MARKET BUY 1 lot ({lot_size} qty) {tradingsymbol}…")
        try:
            oid = kc.place_order(
                variety          = kc.VARIETY_REGULAR,
                exchange         = kc.EXCHANGE_NFO,
                tradingsymbol    = tradingsymbol,
                transaction_type = kc.TRANSACTION_TYPE_BUY,
                quantity         = lot_size,
                product          = kc.PRODUCT_MIS,
                order_type       = kc.ORDER_TYPE_MARKET,
            )
            self.order_id = oid
            self.state    = "ordered"
            self._log(f"Option BUY placed: {oid} · {tradingsymbol}")
        except Exception as e:
            self.state = "error"
            self._log(f"Option order failed: {e}")
            return

        # poll for fill
        self._log("Waiting for fill…")
        for _ in range(30):
            time.sleep(2)
            try:
                orders = kc.orders()
                o = next((x for x in orders if str(x["order_id"]) == str(self.order_id)), None)
                if o and o["status"] == "COMPLETE":
                    self.fill_price = float(o["average_price"])
                    self._log(f"FILLED at ₹{self.fill_price} premium")
                    break
            except Exception:
                pass
        else:
            self._log("Could not confirm fill — exit manually.")
            self.state = "done"
            return

        # SL at 50% of premium paid
        sl_trigger = round(self.fill_price * 0.50, 2)
        self._log(f"Placing SL-M at ₹{sl_trigger} (50% of ₹{self.fill_price} premium)…")
        try:
            sl_oid = kc.place_order(
                variety          = kc.VARIETY_REGULAR,
                exchange         = kc.EXCHANGE_NFO,
                tradingsymbol    = tradingsymbol,
                transaction_type = kc.TRANSACTION_TYPE_SELL,
                quantity         = lot_size,
                product          = kc.PRODUCT_MIS,
                order_type       = kc.ORDER_TYPE_SLM,
                trigger_price    = sl_trigger,
            )
            self.sl_order_id = sl_oid
            self.state       = "done"
            self._log(f"SL-M placed: {sl_oid} trigger=₹{sl_trigger}")
        except Exception as e:
            self.state = "error"
            self._log(f"SL order failed: {e} — EXIT MANUALLY!")

    def _wait_and_order(self, delay=None):
        try:
            self._wait_for_open(delay=delay)
            if not self._stop:
                self._place_order()
        except Exception as e:
            self.state = "error"
            self._log(f"ERROR: {e}")

    def status(self):
        return {
            "state":       self.state,
            "pick":        self.pick,
            "order_id":    self.order_id,
            "sl_order_id": self.sl_order_id,
            "fill_price":  self.fill_price,
            "log":         self.log[-20:],
        }


def _vol_surge(deltas: list) -> int:
    if len(deltas) < 6:
        return 0
    recent = deltas[-11:]
    prev = recent[:-1]
    avg = sum(prev) / len(prev)
    return 1 if avg > 0 and recent[-1] >= 2 * avg else 0


def _score(d):
    t = d.get("ticks", [])
    if len(t) < 3:
        return {"total": 0}
    ratios = [x["ratio"] for x in t]
    ratioNow = ratios[-1]
    ratioTrend = sum(1 if ratios[i] > ratios[i-1] else -1
                     for i in range(max(1, len(ratios)-5), len(ratios)))
    ratioScore = 2 if ratioNow >= 1 else (1 if ratioTrend > 0 else 0)
    dBuys  = [x["dBuy"]  for x in t if x.get("dBuy")  is not None]
    dSells = [x["dSell"] for x in t if x.get("dSell") is not None]
    buyScore  = 1 if dBuys  and sum(1 for x in dBuys  if x > 0) > len(dBuys)  * 0.6 else 0
    sellScore = 1 if dSells and sum(1 for x in dSells if x < 0) > len(dSells) * 0.6 else 0
    absorptions = sum(1 for x in t if x.get("dSell") is not None
                      and x["dSell"] < 0 and x.get("chgPct", 0) > 0)
    absorbScore = 2 if absorptions >= 3 else (1 if absorptions >= 1 else 0)
    prices = [x["ltp"] for x in t[-6:]]
    priceScore = 1 if len(prices) >= 2 and prices[-1] > prices[0] else 0
    volSurge = _vol_surge(dBuys)
    return {"total": ratioScore + buyScore + sellScore + absorbScore + priceScore + volSurge}


# singletons
TRADER = AutoTrader()   # legacy alias → BUY side
BUYER  = AutoTrader()
SELLER = AutoTrader()
