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

# --- daily recommendation ---
REC_PE_MAX = 50            # screen out leaders pricier than this trailing P/E (overvaluation guard)
REC_MIN_EPS = 0            # require trailing EPS strictly above this (drops loss-making / zero-EPS names)
REC_RVOL_MIN = 1.0         # buy MUST show recent (5d) volume >= its 20-day average — participation confirms the move
REC_MAX_EXT_PCT = 18       # > this % above the 50-DMA = overextended -> reversal-risk flag
REC_SKIP_REVERSAL = False  # True = drop reversal-risk names from buy picks; False = keep but warn
REC_VETO_ON_CONFLICT = True   # drop a pick when Claude's news read OPPOSES it (bearish news on a buy / bullish on a sell)
REC_CONFLICT_CONVICTION = 60  # min opposing-news conviction (%) needed to veto the pick

# --- intraday news radar (1-min buy/sell/volatility flashes) ---
NEWS_POLL_SEC = 60         # how often the background radar pulls fresh headlines
NEWS_MAX_AGE_MIN = 180     # only FLASH headlines published within this window (mins)
NEWS_HEADLINES_PER_STOCK = 6  # supporting headlines kept per stock (the expandable list)
NEWS_FEED_KEEP = 40        # flash alerts retained in the rolling per-market feed
NEWS_WATCH_FALLBACK = 6    # if no daily picks cached yet, watch the top N of the universe
NEWS_USE_CLAUDE = False    # escalate strong buy/sell hits to Claude for a conviction read

# --- live mode (per-second recommend + 30s self-grade) ---
LIVE_HORIZON_S = 30        # the recommendation must pay off within this many seconds
LIVE_LOOKBACK_S = 5        # seconds of micro-momentum used to trigger
LIVE_MOM_MIN = 0.0005      # min up-move over the lookback to fire a BUY (0.05%)
LIVE_IMB_MIN = 0.10        # min order-book imbalance (bid-heavy) to fire
LIVE_TARGET = 0.0          # "correct" if best net move in the window clears this (after costs)
LIVE_POLL_SEC = 1.0        # how often we poll quotes (1s)
LIVE_CONSEC_UPS = 5        # (backtest streak mode) consecutive price rises -> BUY
LIVE_WINDOW_SEC = 10       # live rule: rolling window length in seconds
LIVE_MIN_UPS = 6           # recommend BUY if >= this many of the last 10 ticks were up (majority)
