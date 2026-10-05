"""Shared mutable runtime state for the dashboard server.

Everything the refresh loop, the trading engine and the API routes need to share lives here,
accessed as `st.<name>` (see `from oi_dashboard.core import state as st`).
"""
import threading


class _Ts:
    """Tiny attribute holder (used to throttle chain logging)."""
    pass


last_known_spot      = 0.0
last_vix             = 12.0
ic_entry_date        = None   # tracks last date to trigger daily resets (VIX open, etc.)
panic_buy_active     = False
panic_buy_entry_time = None
spot_10min_history   = []
spot_60min_history   = []
strike_session_lows  = {}
panic_buy_date       = None
# ── Deep OTM signal cache ────────────────────────────────────────────────────
deep_otm_pe_signal       = None   # last check_deep_otm_signal result (PE side)
deep_otm_ce_signal       = None   # last check_deep_otm_signal result (CE side)
deep_otm_pe_reason       = ""
deep_otm_ce_reason       = ""
deep_otm_pe_inflations   = []     # top IV inflations for display
deep_otm_ce_inflations   = []
# ── PE/CE Activity Classifier cache ──────────────────────────────────────────
pe_activity_cache        = None   # last classify_pe_activity() result
ce_activity_cache        = None   # last classify_ce_activity() result
spread_decay_cache       = None   # last analyze_spread_decay() result
decay_signal_history: list = []   # rolling last 3 raw combined_signals
decay_signal_stable: str  = "NEUTRAL"  # last confirmed stable signal
decay_stable_ts: str      = ""         # timestamp of last stable signal
activity_log_last_time   = 0.0    # unix ts of last append_activity_log() write
# ── Intraday path awareness ──────────────────────────────────────────────────
minute_history       = []    # [(datetime, price)] rolling 95 min — for intraday vol/chop
micro_moves_today    = []    # [{"time":dt,"move_pts":float,"direction":str,...}]
rv_peak_today        = 0.0   # rolling-RV peak seen today
rv_peak_time_today   = ""    # "HH:MM" when peak occurred
minute_bars_saved    = False  # EOD save guard
cache        = {}
cache_time   = 0
demo_mode    = False
log_chain_ts = _Ts()
last_auto_attempt    = 0.0   # unix ts of last _try_auto_enter call
last_successful_fetch = 0.0  # unix ts of last successful Dhan API fetch
# ── Paper trading state ─────────────────────────────────────────────────────
paper_trades       = []
paper_trade_ids    = set()
paper_trade_date   = None
paper_trade_paused = False   # set True after reset; cleared by resume or bulk_add
pending_entries    = {}      # key → {tick1: prices_dict, time: datetime} for two-tick fill
current_expiry_dt  = ""     # current contract expiry "YYYY-MM-DD", set each cache refresh
auto_activity_log  = []     # last N AUTO events for dashboard mini-panel
mode_at_930        = None   # directional mode captured at first 9:30 classification (today only)
mode_at_930_date   = ""     # date string matching _mode_at_930
reversal_mode      = None   # REVERSAL_UP / REVERSAL_DOWN once detected; resets daily
# Spike detector cross-asset cache
last_banknifty     = 0.0    # last valid BankNifty reading
banknifty_15m_ago  = 0.0    # BankNifty reading ~15 min ago (for correlation check)
bn_history: list   = []     # [(timestamp, ltp), ...] last 30 min
usdinr_now         = None   # USD/INR — None until we get data
usdinr_15m_ago     = None
# ── RV state ────────────────────────────────────────────────────────────────
spot_history       = []    # daily closes for RV computation
spot_history_dates = []    # corresponding date strings
ohlc_history       = []    # [{date,open,high,low,close}] in chronological order — for YZ
vol_audit_printed  = False
rv_cache           = {}    # {date_str: {"rv5": x, "rv10": x, "rv20": x, "rv60": x}}
rv20_cache         = {}    # kept for backward compatibility
last_rv_date       = None
open_overpricing      = {}    # {strike: {"ce_ovr": pct, "pe_ovr": pct}}
last_market_condition = "WAIT"  # updated when _classify_market runs at 9:30am
last_mode_reason      = ""      # human-readable classifier caption for the header
skew_logged_today     = ""      # date string; log_skew_history runs once per day ~14:00
# Gap confidence tracking
vix_at_open        = 0.0   # first VIX reading after 9:15 IST; reset each day
vix_open_date      = ""    # date string for the above
last_gap_confidence = {}   # most recent score dict for cache/header
open_ltp_snapshot     = {}      # {strike: {"ce_ltp": x, "pe_ltp": x}} — first fetch after 9:15
gap_stats             = {}      # overnight gap percentiles from spot_all.csv
iv_crush_log          = []      # daily IV crush history [{date, open_iv, close_iv, crush_pct}]
iv_crush_today_snap   = {}      # {"open_iv": x, "open_spot": x} captured at ~9:20
carry_prompt_pending  = False   # set True at 15:25 when OPEN trades exist; cleared after action
# Events calendar
events_map   = {}   # {date_str: {"type", "label", "action"}} — highest-priority action wins per day
events_list  = []   # raw list for upcoming-banner lookup
# ============================================================
#  MANUAL JOURNAL  (in-memory, survives session)
# ============================================================
journal_trades = []
journal_id_counter = 0
sensex_state = {
    'data':        None,
    'last_update': None,
    'error':       None,
}
sensex_lock = threading.Lock()
