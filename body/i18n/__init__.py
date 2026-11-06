"""Two languages from day one: Spanish (Colombia) is the default; English is a full version.

Every word on screen comes from es.json / en.json. A missing key in English falls back to Spanish, and a key
missing in both shows the key itself (so it is noticed, never silently blank).
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

LANGS = ("es", "en")
DEFAULT = "es"
_DIR = Path(__file__).parent


@lru_cache
def _table(lang: str) -> dict:
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


class T:
    def __init__(self, lang: str):
        self.lang = lang if lang in LANGS else DEFAULT

    def __call__(self, key: str, **kw) -> str:
        val = _table(self.lang).get(key)
        if val is None:
            val = _table(DEFAULT).get(key, key)
        return val.format(**kw) if kw and isinstance(val, str) else val

    # ---- number and date formatting per language
    def num(self, v, decimals: int = 0) -> str:
        if v is None:
            return "—"
        s = f"{v:,.{decimals}f}"
        if self.lang == "es":  # 10.432 · 4,5
            s = s.replace(",", "\0").replace(".", ",").replace("\0", ".")
        return s

    def minutes(self, v) -> str:
        if v is None:
            return "—"
        h, m = divmod(int(round(v)), 60)
        return f"{h} h {m:02d}" if h else f"{m} min"

    def long_date(self, d: date) -> str:
        return self("date.long", weekday=self("date.days")[d.weekday()], day=d.day,
                    month=self("date.months")[d.month - 1])
