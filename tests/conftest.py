"""Pytest configuration for climate_manager tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Make custom_components/ importable as a package
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
