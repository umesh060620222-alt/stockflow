"""Mock Kite WebSocket server for pre-market testing.

Sends properly-formatted Kite v3 binary packets so the real parser
in index.html can be validated before market hours.

Packet layout (44-byte quote mode, big-endian int32s):
  offset  0 : instrument_token
  offset  4 : last_price   * 100
  offset  8 : last_qty
  offset 12 : avg_price    * 100
  offset 16 : volume
  offset 20 : total_buy_qty        ← buyVol
  offset 24 : total_sell_qty       ← sellVol
  offset 28 : ohlc.open   * 100   ← EIP (indicative opening price)
  offset 32 : ohlc.high   * 100
  offset 36 : ohlc.low    * 100
  offset 40 : ohlc.close  * 100   ← prev close

Run:  python mock_kite_ws.py
Then in the browser pre-market section click "Mock WS" (or set URL to ws://localhost:8765)
"""
import asyncio, struct, random, json, sys

try:
    import websockets
except ImportError:
    print("Install websockets:  pip install websockets")
    sys.exit(1)

HOST = "localhost"
PORT = 8765
TOKEN = 341249   # arbitrary test token — matches what we'll subscribe

PREV_CLOSE = float(sys.argv[1]) if len(sys.argv) > 1 else 1200.0


def make_packet(token, ltp, buy, sell, eip, prev_close):
    """44-byte Kite quote packet."""
    def i(v): return max(-2**31, min(2**31-1, int(round(v))))
    data = struct.pack(">iiiiiiiiiii",
        i(token),
        i(ltp * 100),
        100,                    # last_qty
        i(ltp * 100),           # avg_price
        i(buy + sell),          # volume
        i(buy),                 # total_buy_qty  ← offset 20
        i(sell),                # total_sell_qty ← offset 24
        i(eip * 100),           # ohlc.open      ← offset 28  (EIP)
        i(ltp * 100),           # ohlc.high
        i(ltp * 100),           # ohlc.low
        i(prev_close * 100),    # ohlc.close     ← prev close
    )
    # wrap: [int16 n_packets=1][int16 pkt_len][data]
    return struct.pack(">hh", 1, len(data)) + data


async def stream(ws):
    print(f"  client connected: {ws.remote_address}")
    ltp = PREV_CLOSE

    # drain any subscribe/mode messages without blocking the stream
    async def drain():
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    print(f"  <- {msg}")
                except Exception:
                    pass
        except Exception:
            pass

    asyncio.create_task(drain())

    for tick in range(420):
        buy  = random.randint(50_000, 800_000)
        sell = random.randint(50_000, 800_000)
        imbalance = (buy - sell) / (buy + sell)   # -1..+1
        ltp  *= (1 + imbalance * 0.0005)
        eip   = round(ltp, 2)

        pkt = make_packet(TOKEN, round(ltp, 2), buy, sell, eip, PREV_CLOSE)
        try:
            await ws.send(pkt)
            gap = (eip - PREV_CLOSE) / PREV_CLOSE * 100
            ratio = buy / sell if sell else 0
            print(f"  tick {tick+1:3d}  LTP={ltp:.2f}  EIP={eip:.2f}  "
                  f"gap={gap:+.2f}%  buy={buy:,}  sell={sell:,}  ratio={ratio:.2f}")
        except Exception:
            break
        await asyncio.sleep(1)

    print("  stream done")


async def main():
    print(f"Mock Kite WS  →  ws://{HOST}:{PORT}")
    print(f"Token={TOKEN}   PrevClose={PREV_CLOSE}")
    print("Waiting for browser connection…\n")
    async with websockets.serve(stream, HOST, PORT):
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
