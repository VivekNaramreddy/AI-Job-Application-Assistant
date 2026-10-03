"""Run unittest discovery with temporary storage, never the user's data."""

import os
import sys
import tempfile
import unittest
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="job-applier-tests-") as folder:
        os.environ["JOB_APPLIER_DATA_DIR"] = str(Path(folder) / "data")
        os.environ["JOB_APPLIER_EXPORTS_DIR"] = str(Path(folder) / "exports")
        tests = unittest.defaultTestLoader.discover(
            str(Path(__file__).resolve().parent / "tests"),
            pattern=sys.argv[1] if len(sys.argv) > 1 else "test*.py",
        )
        result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(tests)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
