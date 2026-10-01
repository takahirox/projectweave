"""Serve the real dashboard assets for browser tests; tests supply API fixtures."""
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from projectweave.dashboard import Dashboard
from projectweave.execution import ExecutionRegistry

with Dashboard(ExecutionRegistry(), port=8766):
    threading.Event().wait()
