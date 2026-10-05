"""Flask application factory and startup bootstrap."""
from flask import Flask
from flask_cors import CORS

from oi_dashboard.analytics.events_calendar import compute_gap_stats, load_events
from oi_dashboard.analytics.realized_vol import load_spot_history
from oi_dashboard.api import ALL_BLUEPRINTS
from oi_dashboard.core.constants import PAPER_TRADES_FILE, SPOT_HISTORY_FILE
from oi_dashboard.services.leg_tracker import start_leg_tracking_thread
from oi_dashboard.services.sensex_worker import start_sensex_thread
from oi_dashboard.storage.session_store import (
    load_iv_crush_log,
    load_open_ltp_snapshot,
    load_paper_trades,
)

_bootstrapped = False


def bootstrap():
    """Load persisted state, initialise the database and start background workers (once)."""
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True

    load_paper_trades()

    try:
        from oi_dashboard.storage import db
        db.init_db()
        db.migrate_json_to_db(PAPER_TRADES_FILE)
        try:
            db.seed_spot_daily_from_csv(SPOT_HISTORY_FILE)
        except Exception:
            pass
    except Exception as err:
        print(f"[DB] Non-fatal init error: {err}")

    load_spot_history()         # 65 days of spot history for accurate RV from minute one
    load_open_ltp_snapshot()    # LTP snapshot from session open for the change-from-open column
    load_iv_crush_log()         # historical IV crush records
    compute_gap_stats()         # overnight gap percentiles from spot history
    load_events()               # events calendar: RBI MPC, Fed FOMC, CPI, expiries
    start_leg_tracking_thread()
    start_sensex_thread()


def create_app():
    app = Flask(__name__)
    CORS(app, origins="*")
    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)
    bootstrap()
    return app
