"""Mock Kite WebSocket server for pre-market testing.

Simulates a BUY scenario:
  Phase 1 (ticks 1-40):  strong uptrend, high buy ratio  → classifies as 'Buying'
  Phase 2 (ticks 41-90): pullback toward midpoint        → entry fires
  Phase 3 (ticks 91+):   resume up                       → target hit

Packet layout (44-byte quote mode, big-endian int32s):
  offset  0 : instrument_token
  offset  4 : last_price   * 100
  offset 20 : total_buy_qty
  offset 24 : total_sell_qty
  offset 40 : ohlc.close  * 100   ← prev close

Run:  python mock_kite_ws.py [prev_close]
"""
import asyncio, struct, random, json, sys

try:
    import websockets
except ImportError:
    print("Install websockets:  pip install websockets"); sys.exit(1)

HOST = "localhost"
PORT = 8765
TOKEN = 341249

PREV_CLOSE = float(sys.argv[1]) if len(sys.argv) > 1 else 1200.0


def make_packet(token, ltp, buy, sell, prev_close):
    def i(v): return max(-2**31, min(2**31-1, int(round(v))))
    data = struct.pack(">iiiiiiiiiii",
        i(token),
        i(ltp * 100),   # last_price
        100,            # last_qty
        i(ltp * 100),   # avg_price
        i(buy + sell),  # volume
        i(buy),         # total_buy_qty  ← offset 20
        i(sell),        # total_sell_qty ← offset 24
        i(ltp * 100),   # ohlc.open
        i(ltp * 100),   # ohlc.high
        i(ltp * 100),   # ohlc.low
        i(prev_close * 100),  # ohlc.close ← prev close, offset 40
    )
    return struct.pack(">hh", 1, len(data)) + data


def phase_params(tick, ltp, prev_close):
    """Return (price_delta_pct, buy_vol, sell_vol) for current tick."""
    if tick <= 40:
        # uptrend: price +0.06%/tick, strong buy dominance
        dp  = 0.0006 + random.uniform(-0.0001, 0.0001)
        buy = random.randint(400_000, 700_000)
        sell= random.randint(80_000, 180_000)   # ratio ~3-5
    elif tick <= 90:
        # pullback: price -0.04%/tick, buy still dominant (won't flip to Selling)
        dp  = -0.0004 + random.uniform(-0.0001, 0.0001)
        buy = random.randint(300_000, 500_000)
        sell= random.randint(150_000, 280_000)  # ratio ~1.5-2.5, stays > 0.8
    else:
        # resume up: price +0.05%/tick
        dp  = 0.0005 + random.uniform(-0.0001, 0.0001)
        buy = random.randint(350_000, 600_000)
        sell= random.randint(100_000, 200_000)
    return dp, buy, sell


async def stream(ws):
    print(f"  client connected")
    ltp = PREV_CLOSE

    async def drain():
        try:
            async for raw in ws:
                try: print(f"  <- {json.loads(raw)}")
                except Exception: pass
        except Exception: pass

    asyncio.create_task(drain())

    for tick in range(1, 421):
        dp, buy, sell = phase_params(tick, ltp, PREV_CLOSE)
        ltp = round(ltp * (1 + dp), 2)
        chg = (ltp - PREV_CLOSE) / PREV_CLOSE * 100
        ratio = buy / sell
        phase = "UP  " if tick <= 40 else ("PULL" if tick <= 90 else "RSME")
        pkt = make_packet(TOKEN, ltp, buy, sell, PREV_CLOSE)
        try:
            await ws.send(pkt)
            print(f"  [{phase}] tick {tick:3d}  LTP={ltp:.2f}  chg={chg:+.2f}%  "
                  f"buy={buy:,}  sell={sell:,}  ratio={ratio:.2f}")
        except Exception:
            break
        await asyncio.sleep(1)

    print("  stream done")


async def main():
    print(f"Mock Kite WS  →  ws://{HOST}:{PORT}")
    print(f"Token={TOKEN}   PrevClose={PREV_CLOSE}")
    print("Phases: UP (1-40) → PULLBACK (41-90) → RESUME (91+)")
    print("Waiting for browser connection…\n")
    async with websockets.serve(stream, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
