"""Mock Kite WebSocket — BUY stock + SELL stock, 3 cycles each.

BUY  stock (token 341249):  UP→PULL→RSME  ×3  (60 ticks/cycle)
SELL stock (token 408065):  DOWN→BOUNCE→RSME_DOWN  ×3  (60 ticks/cycle)

Run:  python mock_kite_ws.py [prev_close]
"""
import asyncio, struct, random, json, sys

try:
    import websockets
except ImportError:
    print("pip install websockets"); sys.exit(1)

HOST = "localhost"
PORT = 8765
TOKEN_BUY  = 341249
TOKEN_SELL = 408065
PREV_CLOSE = float(sys.argv[1]) if len(sys.argv) > 1 else 1200.0


def make_packet(token, ltp, buy, sell, prev_close):
    def i(v): return max(-2**31, min(2**31-1, int(round(v))))
    data = struct.pack(">iiiiiiiiiii",
        i(token),
        i(ltp * 100),
        100,
        i(ltp * 100),
        i(buy + sell),
        i(buy),
        i(sell),
        i(ltp * 100),
        i(ltp * 100),
        i(ltp * 100),
        i(prev_close * 100),
    )
    return struct.pack(">h", len(data)) + data


def buy_phase(tick):
    pos = (tick - 1) % 60
    if pos < 40:
        return 0.0006 + random.uniform(-0.00005, 0.00005), \
               random.randint(400_000, 700_000), random.randint(80_000,  160_000), "BUY-UP  "
    elif pos < 45:
        return -0.0002 + random.uniform(-0.00005, 0.00005), \
               random.randint(350_000, 550_000), random.randint(120_000, 220_000), "BUY-PULL"
    else:
        return 0.0006 + random.uniform(-0.00005, 0.00005), \
               random.randint(400_000, 650_000), random.randint(90_000,  170_000), "BUY-RSME"


def sell_phase(tick):
    pos = (tick - 1) % 60
    if pos < 40:
        return -0.0006 + random.uniform(-0.00005, 0.00005), \
               random.randint(80_000,  160_000), random.randint(400_000, 700_000), "SEL-DOWN"
    elif pos < 45:
        return 0.0002 + random.uniform(-0.00005, 0.00005), \
               random.randint(120_000, 220_000), random.randint(350_000, 550_000), "SEL-BNCE"
    else:
        return -0.0006 + random.uniform(-0.00005, 0.00005), \
               random.randint(90_000,  170_000), random.randint(400_000, 650_000), "SEL-RSME"


async def stream(ws):
    print("  client connected")
    ltp_buy  = PREV_CLOSE
    ltp_sell = PREV_CLOSE

    async def drain():
        try:
            async for raw in ws:
                try: print(f"  <- {json.loads(raw)}")
                except Exception: pass
        except Exception: pass

    asyncio.create_task(drain())

    for tick in range(1, 421):
        db, bb, sb, lb = buy_phase(tick)
        ds, bs, ss, ls = sell_phase(tick)
        ltp_buy  = round(ltp_buy  * (1 + db), 2)
        ltp_sell = round(ltp_sell * (1 + ds), 2)

        pkt = make_packet(TOKEN_BUY,  ltp_buy,  bb, sb, PREV_CLOSE) + \
              make_packet(TOKEN_SELL, ltp_sell, bs, ss, PREV_CLOSE)
        try:
            await ws.send(pkt)
            chg_b = (ltp_buy  - PREV_CLOSE) / PREV_CLOSE * 100
            chg_s = (ltp_sell - PREV_CLOSE) / PREV_CLOSE * 100
            print(f"  [{lb}] LTP={ltp_buy:.2f} chg={chg_b:+.2f}% r={bb/sb:.2f}  "
                  f"[{ls}] LTP={ltp_sell:.2f} chg={chg_s:+.2f}% r={bs/ss:.2f}")
        except Exception:
            break
        await asyncio.sleep(1)
    print("  stream done")


async def main():
    print(f"Mock Kite WS → ws://{HOST}:{PORT}  PrevClose={PREV_CLOSE}")
    print(f"BUY token={TOKEN_BUY}   SELL token={TOKEN_SELL}")
    print("Each cycle 60 ticks: 40 trend → 5 pullback/bounce → 15 resume\n")
    async with websockets.serve(stream, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
