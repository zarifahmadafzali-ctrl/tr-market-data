#!/usr/bin/env python3
"""GitHub Actions entrypoint for the durable download engine (Phase 1).

Usage:
  python -m scripts.actions_engine --once
  python -m scripts.actions_engine --request-id <id>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.run import main_argv

if __name__ == "__main__":
    raise SystemExit(main_argv())
