"""Evaluation: scenarios, simulated world, baselines, agent runs.

Importing anything from `evaluation` puts `backend/` on the import path, so
evaluation code can use `app.agent...` exactly like the backend does.
"""
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parent.parent / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
