"""Mock Kite WebSocket server for pre-market testing.

Phases:
  UP   (ticks 1-40):  uptrend +0.06%/tick, buy ratio 3-6  → classifies as Buying
  PULL (ticks 41-45): dip -0.02%/tick for 5 ticks         → midpoint entry fires
  RSME (ticks 46+):   resume +0.06%/tick                  → target hit ~9 ticks later

Run:  python mock_kite_ws.py [prev_close]
"""
import asyncio, struct, random, json, sys

try:
    import websockets
except ImportError:
    print("pip install websockets"); sys.exit(1)

HOST = "localhost"
PORT = 8765
TOKEN = 341249
PREV_CLOSE = float(sys.argv[1]) if len(sys.argv) > 1 else 1200.0


def make_packet(token, ltp, buy, sell, prev_close):
    def i(v): return max(-2**31, min(2**31-1, int(round(v))))
    data = struct.pack(">iiiiiiiiiii",
        i(token),
        i(ltp * 100),
        100,
        i(ltp * 100),
        i(buy + sell),
        i(buy),              # total_buy_qty  offset 20
        i(sell),             # total_sell_qty offset 24
        i(ltp * 100),
        i(ltp * 100),
        i(ltp * 100),
        i(prev_close * 100), # prev close     offset 40
    )
    return struct.pack(">h", len(data)) + data   # [int16 pkt_len][data]


def phase_params(tick):
    # cycle: 40 UP → 5 PULL → 15 RSME = 60 ticks per trade
    pos = (tick - 1) % 60
    if pos < 40:
        dp   = 0.0006 + random.uniform(-0.00005, 0.00005)
        buy  = random.randint(400_000, 700_000)
        sell = random.randint(80_000,  160_000)
        label = "UP  "
    elif pos < 45:
        dp   = -0.0002 + random.uniform(-0.00005, 0.00005)
        buy  = random.randint(350_000, 550_000)
        sell = random.randint(120_000, 220_000)
        label = "PULL"
    else:
        dp   = 0.0006 + random.uniform(-0.00005, 0.00005)
        buy  = random.randint(400_000, 650_000)
        sell = random.randint(90_000,  170_000)
        label = "RSME"
    return dp, buy, sell, label


async def stream(ws):
    print("  client connected")
    ltp = PREV_CLOSE

    async def drain():
        try:
            async for raw in ws:
                try: print(f"  <- {json.loads(raw)}")
                except Exception: pass
        except Exception: pass

    asyncio.create_task(drain())

    for tick in range(1, 421):
        dp, buy, sell, label = phase_params(tick)
        ltp = round(ltp * (1 + dp), 2)
        chg = (ltp - PREV_CLOSE) / PREV_CLOSE * 100
        pkt = make_packet(TOKEN, ltp, buy, sell, PREV_CLOSE)
        try:
            await ws.send(pkt)
            print(f"  [{label}] tick {tick:3d}  LTP={ltp:.2f}  chg={chg:+.2f}%  "
                  f"buy={buy:,}  sell={sell:,}  ratio={buy/sell:.2f}")
        except Exception:
            break
        await asyncio.sleep(1)
    print("  stream done")


async def main():
    print(f"Mock Kite WS → ws://{HOST}:{PORT}  PrevClose={PREV_CLOSE}")
    print("UP (1-40) → PULL (41-45) → RSME (46+)\n")
    async with websockets.serve(stream, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
