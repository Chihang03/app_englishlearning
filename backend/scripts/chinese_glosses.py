"""CLI compatibility exports for shared Chinese definition supplements."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.chinese_glosses import DEFAULT_SUPPLEMENTS, apply_supplements
