import zerodha as Z
import datetime as dt

def run():
    kc = Z.kite()
    insts = Z.get_nfo_instruments(kc)
    if not insts:
        print("Failed to get NFO instruments.")
        return
        
    nifty_expiries = set()
    for inst in insts:
        if inst.get("name") == "NIFTY":
            exp = inst.get("expiry")
            if exp:
                nifty_expiries.add(str(exp))
                
    print("Available Nifty Expiries:")
    for exp in sorted(list(nifty_expiries)):
        print(f"  - {exp}")

if __name__ == "__main__":
    run()
