"""Strategy + universe + cost configuration. Tune these, re-run, compare."""

# --- universe: liquid NSE names (yfinance uses the .NS suffix) ---
# Tight spreads matter at intraday timeframes — these are all highly liquid.
UNIVERSE = [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "ITC.NS",
    "BHARTIARTL.NS", "HINDUNILVR.NS", "BAJFINANCE.NS", "MARUTI.NS", "NTPC.NS",
    "SUNPHARMA.NS", "TITAN.NS", "ADANIENT.NS", "WIPRO.NS", "HCLTECH.NS",
]
BENCHMARK = "^NSEI"          # Nifty 50 — relative-strength reference

# --- data ---
SOURCE = "yfinance"          # deploy-safe default (no creds). Pick "zerodha" in the UI when a token exists.
INTERVAL = "5m"              # "1m" (last 7 days only, finer) or "5m" (robust)
PERIOD = "1d"                # how much history to pull ("1d","5d", up to "7d" for 1m)
TZ = "Asia/Kolkata"

# --- entry mode ---
MODE = "momentum"          # "momentum" (chase the move) or "meanrev" (fade extension to VWAP)
DEVIATION_PCT = 0.003      # meanrev: how far from VWAP (fraction) counts as over-extended

# --- entry signal thresholds ---
VOL_MULT = 2.0              # bar volume must exceed this x the rolling average
VOL_AVG_BARS = 12          # bars used for the rolling average volume
MOM_LOOKBACK = 3           # bars for rate-of-change momentum
RS_MIN = 0.0               # stock must outperform Nifty since open by this (fraction)
SKIP_OPEN_BARS = 3         # ignore the first N bars (opening auction noise)

# --- exit / risk (percentages as fractions: 0.008 = 0.8%) ---
TARGET_PCT = 0.008
STOP_PCT = 0.004
TIME_STOP_MIN = 10         # exit if the move hasn't happened in ~10 min
USE_VWAP_STALL_EXIT = True # exit early if price falls back across VWAP

# --- portfolio risk gates ---
MAX_POSITIONS = 5
DAILY_LOSS_LIMIT_PCT = 0.02   # halt new entries after -2% on the day (sum of trade pnl)
COOLDOWN_MIN = 15             # don't re-enter the same symbol for N min after an exit

# --- costs (round-trip realism — this is what separates +EV from -EV) ---
COST_PCT = 0.0010          # all-in brokerage+STT+exch+GST+stamp, round trip (~0.10%)
SLIPPAGE_PCT = 0.0003      # per side; entries/exits don't fill at the exact print

# --- capital (for P&L reporting only; signal quality is per-trade %) ---
CAPITAL_PER_TRADE = 10000
