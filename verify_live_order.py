import sys
import os

# Ensure stockflow import path is active
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import zerodha as Z

def test_live_order():
    print("Initializing Zerodha client connection...")
    try:
        kc = Z.kite()
        print("Successfully connected to Zerodha!")
    except Exception as e:
        print(f"FAILED to connect to Zerodha: {e}")
        print("Please log in via the web interface ('Connect Zerodha') first.")
        return

    symbol = "RELIANCE"
    print(f"Fetching current LTP for {symbol} to set limit price...")
    try:
        ltp_data = kc.ltp(f"NSE:{symbol}")
        ltp = float(ltp_data[f"NSE:{symbol}"]["last_price"])
        limit_price = round(ltp * 1.02, 1)
        print(f"LTP: Rs. {ltp:.2f} | Limit Price (+2%): Rs. {limit_price:.2f}")
    except Exception as e:
        print(f"Failed to fetch LTP: {e}. Defaulting to Rs. 2400 limit price.")
        limit_price = 2400.0

    print(f"Attempting to place a test MIS LIMIT BUY order for 1 share of {symbol} at Rs. {limit_price}...")
    try:
        order_id = kc.place_order(
            variety          = kc.VARIETY_REGULAR,
            exchange         = kc.EXCHANGE_NSE,
            tradingsymbol    = symbol,
            transaction_type = kc.TRANSACTION_TYPE_BUY,
            quantity         = 1,
            product          = kc.PRODUCT_MIS,
            order_type       = kc.ORDER_TYPE_LIMIT,
            price            = limit_price,
        )
        print(f"SUCCESS! Order placed. Order ID: {order_id}")
    except Exception as e:
        print("API Call communicated with Zerodha successfully, but was rejected by exchange/RMS:")
        print(f"Error Message: {e}")

if __name__ == "__main__":
    test_live_order()
