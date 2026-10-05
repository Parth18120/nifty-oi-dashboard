"""Every module in the package must import cleanly (catches broken wiring after refactors)."""
import importlib
import pkgutil

import oi_dashboard


def test_all_modules_import():
    failures = []
    for info in pkgutil.walk_packages(oi_dashboard.__path__, "oi_dashboard."):
        if info.name == "oi_dashboard.app":
            continue  # importing the factory is fine; calling it starts background workers
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # pragma: no cover
            failures.append(f"{info.name}: {exc!r}")
    assert not failures, failures
