"""
greeks_engine.py
Re-exports everything from greeks.py under the new module name.
Use this for new imports — greeks.py stays untouched.
"""
from oi_dashboard.analytics.greeks import compute_greeks, compute_position_greeks  # noqa: F401

__all__ = ["compute_greeks", "compute_position_greeks"]
