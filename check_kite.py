import zerodha
try:
    kc = zerodha.kite()
    print("Success: Kite is authenticated!")
    # Try fetching Nifty instrument tokens
    from kiteconnect import KiteConnect
    print("Kite user details:", kc.profile())
except Exception as e:
    print("Kite failed:", e)
