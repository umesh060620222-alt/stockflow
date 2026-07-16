import numpy as np
import test_trading_strategy as mod

# 10-tick sample to show what the data looks like
up_p, up_bv, up_sv = mod.gen_uptrend(n=10, seed=42)

print("tick | price   | buyVol | sellVol | ratio | dBuy | dSell")
prev_bv = prev_sv = None
for i, (p, bv, sv) in enumerate(zip(up_p, up_bv, up_sv)):
    ratio = bv/sv if sv else 0
    db = bv - prev_bv if prev_bv is not None else 0
    ds = sv - prev_sv if prev_sv is not None else 0
    print(f"  {i:2d} | {p:7.2f}  | {bv:6d} | {sv:6d}  | {ratio:.2f}  | {db:+5d} | {ds:+5d}")
    prev_bv = bv
    prev_sv = sv

# Now show the full 450-tick scenario: peak, trough, expected entry
print()
up_p2, up_bv2, up_sv2 = mod.gen_uptrend()
peak   = max(up_p2[:150])
trough = min(up_p2[150:300])
mid    = (peak + trough) / 2

print("--- Full 450-tick uptrend scenario ---")
print(f"  Phase1 (0-149) rises to peak:    {peak:.2f}")
print(f"  Phase2 (150-299) pulls back to:  {trough:.2f}")
print(f"  Midpoint (entry level):          {mid:.2f}")
print(f"  Expected BUY entry at:           {mid:.2f}")
print(f"  Expected SL at trough:           {trough:.2f}")
print(f"  Expected TARGET at peak:         {peak:.2f}")
print(f"  Risk:   {mid - trough:.2f}   Reward: {peak - mid:.2f}")
print()
reaches = [i for i, p in enumerate(up_p2[150:300], 150) if p <= mid]
print(f"  Price hits mid ({mid:.2f}) at ticks: {reaches[:5]}" if reaches else "  PROBLEM: price never reaches midpoint during pullback")
print(f"  Min pullback price: {min(up_p2[150:300]):.2f}  (need <= {mid:.2f})")
