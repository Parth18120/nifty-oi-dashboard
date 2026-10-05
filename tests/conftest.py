"""Test configuration: make the project importable and fall back to config.example.py.

The real config.py holds broker credentials and is git-ignored, so on a fresh clone (or in CI)
the example config is loaded under the name ``config`` instead.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if importlib.util.find_spec("config") is None:
    spec = importlib.util.spec_from_file_location("config", ROOT / "config.example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["config"] = module
