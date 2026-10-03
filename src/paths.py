"""Application storage paths; overrides also isolate automated test runs."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JOB_APPLIER_DATA_DIR", BASE_DIR / "data")).resolve()
EXPORTS_DIR = Path(os.environ.get("JOB_APPLIER_EXPORTS_DIR", BASE_DIR / "exports")).resolve()
