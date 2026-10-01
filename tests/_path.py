"""A repó gyökerét és a tests mappát felveszi az import-útvonalra (a próbák bárhonnan futtathatók)."""
import pathlib
import sys

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)
