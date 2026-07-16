"""One-off diagnostic (not part of the app): replicate tdSignal()/tdParseBinary
exactly against live ticks for a symbol, to see the real-time buyer/seller
signal right now. Run for ~25s then exit."""
import sys, time, json
import zerodha as Z
from kiteconnect import KiteTicker

SYM = sys.argv[1] if len(sys.argv) > 1 else "HCLTECH"

kc = Z.kite()
api_key, _ = Z._creds()
access_token = json.load(open(Z.TOKEN_FILE))["access_token"]

ltp_data = kc.ltp(f"NSE:{SYM}")[f"NSE:{SYM}"]
inst_token = ltp_data["instrument_token"]

state = {"sessionOpen": None, "prevBuy": None, "prevSell": None, "ticks": []}

def tdSignal():
    ticks = state["ticks"]
    if len(ticks) < 3:
        return None
    last = ticks[-1]
    chgPct = last["chgPct"]
    ratio = last["ratio"]
    up, dn = chgPct > 0.1, chgPct < -0.1
    if up and ratio >= 1.2:
        return "Buying"
    if dn and ratio <= 0.8:
        return "Selling"
    return None

def on_ticks(ws, ticks):
    for t in ticks:
        ltp = t["last_price"]
        bv = t.get("total_buy_quantity", 0)
        sv = t.get("total_sell_quantity", 0)
        if state["sessionOpen"] is None and ltp > 0:
            state["sessionOpen"] = ltp  # matches JS: base = first tick THIS run, not day's open
        chgPct = (ltp - state["sessionOpen"]) / state["sessionOpen"] * 100 if state["sessionOpen"] else 0
        ratio = bv / sv if sv > 0 else 0
        dBuy = bv - state["prevBuy"] if state["prevBuy"] is not None else None
        dSell = sv - state["prevSell"] if state["prevSell"] is not None else None
        if dBuy != 0 or dSell != 0:
            state["ticks"].append({"ltp": ltp, "chgPct": chgPct, "ratio": ratio})
        state["prevBuy"], state["prevSell"] = bv, sv
        sig = tdSignal()
        print(f"{time.strftime('%H:%M:%S')} ltp={ltp:.1f} sessionOpen={state['sessionOpen']:.1f} "
              f"chg={chgPct:+.3f}% buyVol={bv} sellVol={sv} ratio={ratio:.2f} sig={sig}", flush=True)

def on_connect(ws, response):
    ws.subscribe([inst_token])
    ws.set_mode(ws.MODE_FULL, [inst_token])

kws = KiteTicker(api_key, access_token)
kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.connect(threaded=True)
time.sleep(25)
kws.close()
