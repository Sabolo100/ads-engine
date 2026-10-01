"""A repó gyökerét és a tests mappát felveszi az import-útvonalra (a próbák bárhonnan futtathatók)."""
import os
import pathlib
import sys

os.environ.setdefault("ADS_LOG_LEVEL", "error")          # a próbák csendben futnak (a naplót a próbák maguk vizsgálják, ha kell)
TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)
