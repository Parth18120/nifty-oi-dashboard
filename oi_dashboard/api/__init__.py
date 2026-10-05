"""Flask blueprints (HTTP API)."""
from oi_dashboard.api.backtest import bp as backtest_bp
from oi_dashboard.api.carry import bp as carry_bp
from oi_dashboard.api.journal import bp as journal_bp
from oi_dashboard.api.market import bp as market_bp
from oi_dashboard.api.trades import bp as trades_bp

ALL_BLUEPRINTS = (market_bp, trades_bp, carry_bp, journal_bp, backtest_bp)
