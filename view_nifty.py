import yfinance as yf
import pandas as pd

def main():
    df = yf.download('^NSEI', period='5d', interval='1m', progress=False)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Kolkata")
    
    df_day = df[df.index.date.astype(str) == '2026-07-10']
    
    print("Nifty 50 on July 10:")
    for idx, (t, r) in enumerate(df_day.iterrows()):
        if idx % 10 == 0 or idx < 10:
            open_val = r['Open']
            close_val = r['Close']
            if isinstance(open_val, pd.Series):
                open_val = open_val.iloc[0]
            if isinstance(close_val, pd.Series):
                close_val = close_val.iloc[0]
            print(f"  {t.strftime('%H:%M')}: Open={float(open_val):.2f}, Close={float(close_val):.2f}")

if __name__ == "__main__":
    main()
