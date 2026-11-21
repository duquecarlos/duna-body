"""BODY-PLAN-FASES-5-PULIDO-1-M: the demo's own "unusual night" override (body/demo.py's
_seed_noche_inusual) must survive whichever real calendar day it is seeded on. _seed_periodo's
own 'malestar' and 'lesion' periods sit inside the same 30-day baseline lookback those two
overridden signals (hrv_last_night, sleep_score) are read against, and where those periods land
inside that lookback shifts with "today" (the 'lesion' period's own start reaches back to the most
recent strength_training day, itself a function of the shift cycle's calendar-ABSOLUTE anchor) --
so, before this fix, enough of one signal's already-sparse samples could land on marked days that
its 30-day baseline fell below baselines.MIN_DAYS and the card silently dropped. Reproduced on main
with dias=30 (the same short window test_estados_vacios_2.py, test_hoy_escritorio.py and
test_mock_r2_hoy.py all seed with, none of them pinning `today`) for the real date 2026-11-21.
Checked here across several injected "today" values, including that exact date and a December/
January month boundary, so the card's presence never depends on which day the suite happens to
run on."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, identity, web
from body.i18n import T


def _seed_at(case: unittest.TestCase, hoy: date, dias: int = 30) -> "db.sqlite3.Connection":
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    case.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "t.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    demo._seed(conn, dias=dias, idioma="es", today=hoy)
    conn.commit()
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


class NocheInusualSurvivesTheCalendarTest(unittest.TestCase):
    """The demo's own unusual-night card is present on Hoy regardless of which real day "today"
    happens to be -- including 2026-11-21, the day the suite's own gate first caught this on, and
    a December/January month boundary."""

    FECHAS = [date(2026, 11, 20), date(2026, 11, 21), date(2026, 12, 31), date(2027, 1, 1),
              date(2026, 2, 28)]

    def test_card_present_across_injected_todays(self):
        t = T("es")
        for hoy in self.FECHAS:
            with self.subTest(hoy=hoy.isoformat()):
                _seed_at(self, hoy)
                r = TestClient(web.app, follow_redirects=False).get("/")
                self.assertEqual(r.status_code, 200)
                self.assertIn(t("today.noche_inusual_titulo"), r.text,
                              f"the unusual-night card is missing for hoy={hoy.isoformat()}")


if __name__ == "__main__":
    unittest.main()
