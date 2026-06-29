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

    def _place_order(self):
        sym  = self.pick["symbol"]
        qty  = self.pick.get("quantity", 1)
        side = self.pick.get("side", "BUY").upper()
        is_buy = side == "BUY"

        if PAPER:
            self._log(f"[PAPER] MARKET {side} {qty}×{sym}")
            self.order_id = "PAPER-001"
            self.state = "ordered"
            time.sleep(2)
            import random
            self.fill_price = round(self.pick.get("ltp", 1000) * (1 + random.uniform(-0.001, 0.001)), 2)
            self._log(f"[PAPER] FILLED at ₹{self.fill_price} (simulated)")
            sl_trigger = round(self.fill_price * (1 - SL_PCT if is_buy else 1 + SL_PCT), 2)
            self.sl_order_id = "PAPER-002"
            self.state = "done"
            self._log(f"[PAPER] SL-M at ₹{sl_trigger} (simulated)")
            return

        import zerodha as Z
        kc = Z.kite()
        tx    = kc.TRANSACTION_TYPE_BUY  if is_buy else kc.TRANSACTION_TYPE_SELL
        sl_tx = kc.TRANSACTION_TYPE_SELL if is_buy else kc.TRANSACTION_TYPE_BUY
        self._log(f"Placing MARKET {side} for {qty}×{sym}…")
        try:
            oid = kc.place_order(
                variety          = kc.VARIETY_REGULAR,
                exchange         = kc.EXCHANGE_NSE,
                tradingsymbol    = sym,
                transaction_type = tx,
                quantity         = qty,
                product          = kc.PRODUCT_MIS,
                order_type       = kc.ORDER_TYPE_MARKET,
            )
            self.order_id = oid
            self.state = "ordered"
            self._log(f"{side} order placed: {oid}")
        except Exception as e:
            self.state = "error"
            self._log(f"{side} order failed: {e}")
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
                    self._log(f"FILLED at ₹{self.fill_price}")
                    break
            except Exception:
                pass
        else:
            self._log("Could not confirm fill — place SL manually.")
            self.state = "done"
            return

        # place SL-M
        if is_buy:
            sl_trigger = round(self.fill_price * (1 - SL_PCT), 2)
            sl_desc = f"5% below ₹{self.fill_price}"
        else:
            sl_trigger = round(self.fill_price * (1 + SL_PCT), 2)
            sl_desc = f"5% above ₹{self.fill_price}"
        self._log(f"Placing SL-M at ₹{sl_trigger} ({sl_desc})…")
        try:
            sl_oid = kc.place_order(
                variety          = kc.VARIETY_REGULAR,
                exchange         = kc.EXCHANGE_NSE,
                tradingsymbol    = sym,
                transaction_type = sl_tx,
                quantity         = qty,
                product          = kc.PRODUCT_MIS,
                order_type       = kc.ORDER_TYPE_SLM,
                trigger_price    = sl_trigger,
            )
            self.sl_order_id = sl_oid
            self.state = "done"
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
    return {"total": ratioScore + buyScore + sellScore + absorbScore + priceScore}


# singletons
TRADER = AutoTrader()   # legacy alias → BUY side
BUYER  = AutoTrader()
SELLER = AutoTrader()
