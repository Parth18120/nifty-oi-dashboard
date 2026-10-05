"""Filesystem locations used across the project."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
