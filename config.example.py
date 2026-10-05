# Copy this file to config.py and fill in your own Dhan credentials.
# config.py is git-ignored and must never be committed.

# Dhan API credentials
CLIENT_ID    = "YOUR_DHAN_CLIENT_ID"      # from your Dhan account
ACCESS_TOKEN = "YOUR_DHAN_ACCESS_TOKEN"   # regenerate daily from the Dhan app

AUTO_TRADE_ENABLED = True   # enables the paper-trading auto-entry engine (no real orders are ever placed)

# Nifty lot size — NSE revised to 65 effective Jan 2026 (was 75)
LOT_SIZE       = 65
MARGIN_PER_LOT = 91_000    # ~₹91k per lot (was ~₹1,05,000 at 75)

# Display settings
REFRESH_SECONDS  = 60       # auto-refresh interval
STRIKES_TO_SHOW  = 20       # how many strikes each side of ATM
UNDERLYING       = "NIFTY"
UNDERLYING_ID    = "13"   # Nifty spot security ID

# Your positions -- fill these in before market opens
# Leave empty list if no active position
MY_POSITIONS = [
    #{"type": "SELL", "option": "CE", "strike": 24500, "entry_price": 4.25, "qty": 2},
    #{"type": "SELL", "option": "PE", "strike": 23500, "entry_price": 4.15, "qty": 2},
]

# Greeks model inputs
RISK_FREE_RATE  = 0.065   # 6.5% India risk-free rate
DIVIDEND_YIELD  = 0.012   # 1.2% Nifty dividend yield

# Daily spot history used for realised-volatility computation on server startup
# Set to None to accumulate from live data only (takes ~20 trading days)
SPOT_HISTORY_CSV = "data/spot_all.csv"

# Alerts
ALERT_PCR_HIGH = 1.4   # high PCR = bullish for you, not fear
ALERT_PCR_LOW  = 0.7   # low PCR = bearish for you
ALERT_IV_SPIKE_PCT  = 20     # alert when IV spikes more than this % in one refresh
ALERT_OI_BUILD_MIN  = 100000 # alert when OI at any strike builds by this much in one bar

# Best-strikes sweet zone (OTM distance in points) scaled by DTE
# Only sell legs in this band — never closer than DANGER_ZONE_MAX_OTM
SWEET_ZONE_BY_DTE = {
    7: (500, 700),
    6: (480, 680),
    5: (450, 650),
    4: (400, 600),
    3: (350, 550),
    2: (300, 450),
    1: (300, 450),
}
DANGER_ZONE_MAX_OTM = 300    # hard reject any sell leg closer than this
OPPORTUNITY_ZONE    = (200, 400)  # RANGE mode only — wider capture zone allowed

# Deep OTM safe zone — separate from sweet zone (>1000pt away, inflated IV capture)
DEEP_OTM_ZONE_BY_DTE = {
    7: (1000, 1500),
    6: (950,  1450),
    5: (900,  1400),
    4: (850,  1300),
    3: (800,  1200),
    2: (750,  1100),
    1: (700,  1000),
}
DEEP_OTM_MIN_IV_INFLATION = 3.0   # IV must be 3%+ above ATM IV (in pp)
DEEP_OTM_MIN_LTP          = 2.0   # ₹2 minimum sell-leg credit
DEEP_OTM_MAX_LTP          = 15.0  # ₹15 max (too close if higher)
DEEP_OTM_MAX_LOTS         = 15    # hard cap on position size
DEEP_OTM_HEDGE_REQUIRED   = True  # always hedge with deeper OTM buy
DEEP_OTM_HEDGE_OFFSET     = 300   # buy leg this many pts further OTM than sell leg
DEEP_OTM_TARGET_DECAY     = 0.70  # exit at 70% credit decay (deep OTM decays fast)
DEEP_OTM_STOP_MULT        = 3.0   # stop if net credit reaches 3× entry
DEEP_OTM_MIN_SIGNALS      = 2     # require ≥2 strikes with inflation to fire signal
DEEP_OTM_MAX_VIX          = 20.0  # no entry when VIX above this
DEEP_OTM_EVENT_BLOCK_DAYS = 3     # no entry if event within this many days

# Per-trade stop is 2× net credit for spreads (set in _enter_* functions, not here).

# DTE-scaled profit targets — how much of net credit must DECAY before we take profit
# e.g. 0.40 means exit when net credit drops to 60% of entry (60% captured = 40% gone)
TARGET_DECAY_BY_DTE = {7: 0.30, 6: 0.35, 5: 0.40, 4: 0.50, 3: 0.55, 2: 0.60, 1: 0.65}
TARGET_DECAY_DEFAULT = 0.50   # fallback if DTE not in table (was 0.40)

# Trailing stop from peak
TRAILING_ACTIVATION_DECAY = 0.25   # start trailing once 25% of entry credit has decayed
TRAILING_GIVEBACK_PCT      = 0.30   # allow net to rise by 30% of entry credit from best

# Directional guard — when to enter single-side spreads vs IC
GAP_UP_MIN_PCT   = 0.30   # open must be 0.3%+ above prev close for GAP_UP classification
GAP_DOWN_MIN_PCT = 0.30   # open must be 0.3%+ below prev close for GAP_DOWN classification

# Reversal detection
REVERSAL_ENABLED    = True
REVERSAL_MAX_TIME   = "12:00"   # no reversal detection after this time
REVERSAL_BREAK_PTS  = 20        # points beyond prev_close to confirm reversal

# ─── Sensex calibration ──────────────────────────────────────────────────────
# Sensex realized vol runs ~1.4x Nifty in absolute terms.
# Adjust if Sensex OVR% consistently reads too high or too low.
SENSEX_VOL_MULTIPLIER = 1.4

# ─── Realistic Fair Value ────────────────────────────────────────────────────
# Nifty IV historically trades 2-4pp above realized vol in normal conditions.
# Using pure RV as fair value overstates OVR%. Add structural premium for honest signal.
STRUCTURAL_IV_PREMIUM_PP = 0.03   # 3 percentage points (0.03 in decimal)

# Signal thresholds applied to REALISTIC OVR (LTP vs RV+structural fair)
# Corresponds roughly to: STRONG ~30pp, CONSIDER ~15pp in realistic space
# (a 40% theoretical OVR ≈ 25-30% realistic with 3pp structural premium)
OVR_SIGNAL_STRONG   = 30   # realistic OVR% for STRONG label
OVR_SIGNAL_CONSIDER = 15   # realistic OVR% for CONSIDER label
OVR_SIGNAL_SLIGHT   =  5   # realistic OVR% for SLIGHT label

# IC feasibility gate
IC_MIN_OVR_PCT = 0.0            # both sides need OVR > this; raise to 5.0 for stricter

# Emergency override: bypass IC feasibility check (use if IC_FEASIBILITY is blocking despite green signals)
# When True, AUTO will enter IC as long as path/activity gates pass, skipping OVR/LTP/zone checks.
BYPASS_IC_FEASIBILITY = False

# SKIP behavior when IC infeasible in non-directional mode
SKIP_ON_INFEASIBLE_NON_DIRECTIONAL = True   # False = fall back to single-side (NOT recommended)

# Spike detection thresholds — tune after 20+ spikes of data
SPIKE_MOVE_PCT_THRESHOLD     = 0.4   # NIFTY %move to trigger spike detection
SPIKE_MOVE_WINDOW_MIN        = 15    # rolling window in minutes
SPIKE_ACTIVE_DURATION_MIN    = 30    # how long spike stays "active" for trade protection
SPIKE_SL_PAUSE_MIN           = 15    # SL pause duration post-spike (MICRO verdicts only)
SPIKE_VERDICT_MIN_CONFIDENCE = 4     # min signals needed for LIKELY_* verdict

# Real transaction costs per trade (both legs combined, per lot of 75)
COSTS = {
    "brokerage_per_order":  20,       # flat ₹20 per order (entry + exit = 4 orders for strangle)
    "stt_sell_premium_pct": 0.0625,   # 0.0625% of sell premium on expiry/exercise (sell side only)
    "exchange_txn_pct":     0.053,    # 0.053% of turnover (both sides)
    "sebi_pct":             0.0001,   # 0.0001% of turnover
    "gst_pct":              18.0,     # 18% GST on brokerage + exchange fees
    "stamp_duty_pct":       0.003,    # 0.003% of buy-side premium (buyer pays stamp)
}

# ─── New constants (added for modular architecture — do not duplicate above) ───

# Vol estimation
EWMA_LAMBDA        = 0.94
EWMA_SEED_WINDOW   = 20

# Entry filters
MIN_LTP            = 15.0    # was 8.0 — at ₹8-12 fixed costs eat 28-30% of gross win; ₹15+ drops to ~15%
MAX_LTP            = 80.0
MIN_DISTANCE       = 300
MAX_DISTANCE       = 800
MIN_OI             = 500_000
MAX_SPREAD_PCT     = 8.0
STOP_MULT          = 1.5     # was 2.0 — backtest shows 1.5x SL produces 2.5x better net P&L; avg loss ₹984→~₹650
TARGET_DECAY       = 0.50    # was 0.60 — last 20% of premium has disproportionate gamma risk; exit at 50% is safer
EOD_EXIT_TIME      = "15:20"
AUTO_TRADE_START   = "09:30"
AUTO_TRADE_END     = "11:00"
DEFAULT_LOTS       = 10

# Vetoes (used by risk_engine.session_score)
GAP_VETO_PCT       = 0.8
VIX_VETO           = 20.0
VIX_FLOOR          = 10.0   # below VIX 10, premium too thin; transaction costs eat most of the edge

# Surface / skew history
SURFACE_MIN_HISTORY = 10    # days before surface OVR activates
SKEW_BUCKET_WIDTH   = 75    # points each side of moneyness bucket

# File paths
DB_PATH            = "data/dashboard.db"
EVENTS_JSON        = "data/events.json"

# Fair value guards (referenced by vol_engine)
FAIR_FLOOR         = 0.05
OVR_CAP            = 999.0

# OVR thresholds (plain naming for backtest / paper_engine)
OVR_THRESHOLD      = 40.0

# SD-based minimum OTM distance per side (backtest + scan_signals filter)
# PE needs more buffer — PE stop rate 43% vs CE 18% in backtest; fixed-point is wrong under vol regimes
MIN_DIST_SD_CE     = 1.5   # CE minimum distance in standard deviations
MIN_DIST_SD_PE     = 1.8   # PE minimum distance (higher — left tail is fatter)

# Per-strike IV premium threshold (pp above sigma) for sell signals.
# 1.5pp = a strike whose IV is 1.5 percentage points above sigma_forecast.
# At sigma=9%: 1.5pp => IV 10.5% => meaningful edge over fair vol.
IV_PREMIUM_THRESHOLD_PP = 1.5

# Backtest synthetic IV premium above realized vol.
# Represents the typical market IV > RV spread you observe.
# Set conservatively — real VRP is 2-4pp but use 2pp to be honest.
# This is the only parameter that makes synthetic backtest meaningful.
BT_IV_PREMIUM_PP   = 2.0   # percentage points added to sigma for entry pricing
