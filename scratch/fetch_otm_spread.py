import datetime as dt
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zerodha as Z

def fetch_otm_spread():
    print("Connecting to Zerodha Kite client...")
    kc = Z.kite()
    
    # 1. Fetch current Nifty Spot LTP
    quote = kc.quote(["NSE:NIFTY 50"])
    spot_ltp = quote.get("NSE:NIFTY 50", {}).get("last_price")
    if not spot_ltp:
        print("Error: Could not retrieve Nifty Spot LTP.")
        return
        
    print(f"\n⚡ Current Nifty Spot LTP: {spot_ltp:.2f}")
    
    # 2. Get NFO instruments
    print("Fetching option chain instruments...")
    instruments = kc.instruments("NFO")
    nifty_options = [i for i in instruments if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE")]
    
    today = dt.date.today()
    
    # Group by expiry
    expiries = sorted(list({dt.datetime.strptime(i["expiry"], "%Y-%m-%d").date() for i in nifty_options if i.get("expiry")}))
    future_expiries = [e for e in expiries if e >= today]
    
    print("\nAvailable Expiries:")
    for idx, exp in enumerate(future_expiries[:5]):
        days = (exp - today).days
        print(f"  [{idx}] {exp.strftime('%b %d, %Y')} ({days} days to expiry)")
        
    # We target 2-3 months out: September or October expiry
    # Let's find Sept/Oct expiries
    target_expiries = []
    for exp in future_expiries:
        days = (exp - today).days
        if 40 <= days <= 100: # 1.5 to 3 months
            target_expiries.append(exp)
            
    if not target_expiries:
        # Fallback to the furthest available in next 5
        target_expiries = future_expiries[1:3]
        
    for exp in target_expiries[:2]:
        days = (exp - today).days
        print(f"\n--- Analyzing Expiry: {exp.strftime('%b %d, %Y')} ({days} days) ---")
        
        exp_opts = [i for i in nifty_options if dt.datetime.strptime(i["expiry"], "%Y-%m-%d").date() == exp]
        
        # Bull Put Spread (Sell OTM Put, Buy further OTM Put as protection)
        # Sell Strike: Spot - 8% to 10% OTM
        put_sell_target = spot_ltp * 0.91  # 9% OTM
        put_sell_strike = round(put_sell_target / 100.0) * 100
        put_buy_strike = put_sell_strike - 200 # 200 point wide wing
        
        # Bear Call Spread (Sell OTM Call, Buy further OTM Call as protection)
        # Sell Strike: Spot + 8% to 10% OTM
        call_sell_target = spot_ltp * 1.09 # 9% OTM
        call_sell_strike = round(call_sell_target / 100.0) * 100
        call_buy_strike = call_sell_strike + 200 # 200 point wide wing
        
        # Fetch quotes for these strikes
        inst_map = {}
        for i in exp_opts:
            strike = int(float(i["strike"]))
            it = i["instrument_type"]
            if strike in (put_sell_strike, put_buy_strike) and it == "PE":
                inst_map[f"PE_{strike}"] = i["tradingsymbol"]
            if strike in (call_sell_strike, call_buy_strike) and it == "CE":
                inst_map[f"CE_{strike}"] = i["tradingsymbol"]
                
        symbols_to_quote = [f"NFO:{sym}" for sym in inst_map.values()]
        quotes = kc.quote(symbols_to_quote)
        
        def get_ltp(key):
            sym = inst_map.get(key)
            if not sym:
                return 0.0
            return quotes.get(f"NFO:{sym}", {}).get("last_price", 0.0)
            
        pe_sell_ltp = get_ltp(f"PE_{put_sell_strike}")
        pe_buy_ltp = get_ltp(f"PE_{put_buy_strike}")
        
        ce_sell_ltp = get_ltp(f"CE_{call_sell_strike}")
        ce_buy_ltp = get_ltp(f"CE_{call_buy_strike}")
        
        # Bull Put Spread Calculations
        bp_credit = pe_sell_ltp - pe_buy_ltp
        bp_max_loss = 200.0 - bp_credit
        
        # Bear Call Spread Calculations
        bc_credit = ce_sell_ltp - ce_buy_ltp
        bc_max_loss = 200.0 - bc_credit
        
        print(f"\n🟢 BULL PUT SPREAD (OTM Protection Strategy):")
        print(f"  ▸ Sell NIFTY {exp.strftime('%b').upper()} {put_sell_strike} PE @ ₹{pe_sell_ltp:.2f}")
        print(f"  ▸ Buy  NIFTY {exp.strftime('%b').upper()} {put_buy_strike} PE @ ₹{pe_buy_ltp:.2f} (Insurance Wing)")
        print(f"  💰 Net Credit (Profit Pot): {bp_credit:.2f} points (₹{bp_credit * 75:.2f} per lot)")
        print(f"  🛡️ Max Loss:                {bp_max_loss:.2f} points (₹{bp_max_loss * 75:.2f} per lot)")
        print(f"  📊 Margin Required:         ~₹28,000 (instead of ₹1.1L naked!)")
        
        print(f"\n🔴 BEAR CALL SPREAD (OTM Protection Strategy):")
        print(f"  ▸ Sell NIFTY {exp.strftime('%b').upper()} {call_sell_strike} CE @ ₹{ce_sell_ltp:.2f}")
        print(f"  ▸ Buy  NIFTY {exp.strftime('%b').upper()} {call_buy_strike} CE @ ₹{ce_buy_ltp:.2f} (Insurance Wing)")
        print(f"  💰 Net Credit (Profit Pot): {bc_credit:.2f} points (₹{bc_credit * 75:.2f} per lot)")
        print(f"  🛡️ Max Loss:                {bc_max_loss:.2f} points (₹{bc_max_loss * 75:.2f} per lot)")
        print(f"  📊 Margin Required:         ~₹28,000 (instead of ₹1.1L naked!)")

if __name__ == "__main__":
    fetch_otm_spread()
