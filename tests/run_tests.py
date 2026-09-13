"""Test runner: `python backend/tests/run_tests.py`

Verifies the dependencies are importable first and reports honestly instead of
pretending the suite passed when something is missing.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
for path in (BACKEND, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

REQUIRED = ("flask", "flask_sock", "simple_websocket", "werkzeug")


def main() -> int:
    missing = []
    for module in REQUIRED:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        print("Cannot run tests - missing dependencies: " + ", ".join(missing))
        print("Install them first:  pip install -r backend/requirements.txt")
        return 2

    # firebase-admin is optional for the suite: Firebase is mocked in tests.
    try:
        __import__("firebase_admin")
    except ImportError:
        print("note: firebase-admin is not installed; Firebase verification is mocked in tests")

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern="test_*.py", top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
