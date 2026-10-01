"""Ads Engine – önjáró Google Ads kezelő több projekthez (Pacsi, majd kinaiauto.com, darwinai.hu, polibeli.hu).

A motor a szerveren fut: hirdetést épít, hetente kiértékeli (Google + Umami), és a korlátok között módosít.
A keretet és a kampányok be/kikapcsolását az ember a Google Ads felületén kezeli.
"""
from .version import build_id, label  # noqa: F401
from .version import version as _version

__version__ = _version()
