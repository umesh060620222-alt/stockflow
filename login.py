"""Daily Kite login -> saves an access token for the rest of the day.

Two steps (token expires daily, so do this each morning):
  1. python login.py            -> prints the login URL; open it, log in,
                                   copy the request_token from the redirect page.
  2. python login.py <token>    -> exchanges it and saves today's access token.
"""
import sys
import zerodha as Z


def main():
    from kiteconnect import KiteConnect
    api_key, api_secret = Z._creds()
    if not api_key or not api_secret or "PASTE" in api_secret:
        print("Missing credentials — fill API_KEY and API_SECRET in kite_secrets.py.")
        return
    kc = KiteConnect(api_key=api_key)

    token = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not token:
        print("\n1) Open this URL, log in to Zerodha, copy the request_token from the")
        print("   redirect page, then run:  python login.py <request_token>\n")
        print("   ", kc.login_url(), "\n")
        return
    try:
        sess = kc.generate_session(token, api_secret=api_secret)
    except Exception as e:
        print(f"Login failed: {e}  (request_token is single-use and expires fast — get a fresh one)")
        return
    Z.save_token(sess["access_token"])
    print(f"\nOK: access token saved for today ({sess.get('user_name','')}). Now run:  python run.py")


if __name__ == "__main__":
    main()
