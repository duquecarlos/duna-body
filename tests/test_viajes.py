"""BODY-HISTORIA-VIDA-LUGARES-2-R ruling 0: viajes.dias_de_viaje -- a trip counts only at 2+ days,
clipped to the asked range, scoped to one person. Invented trips only, written directly into
viaje_lugares (the same shape body/lugares.py's own sincronizar writes)."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, viajes


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "v.db")
    db.init(conn)  # seeds person id 1 ('Me', DEFAULT_PERSON_ID)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _viaje(conn, person_id, ref, starts_at, ends_at, fetched_at="2026-11-18T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO viaje_lugares (person_id, ref, starts_at, ends_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (person_id, ref, starts_at, ends_at, fetched_at),
    )


class DiasDeViajeTest(unittest.TestCase):
    def test_a_5_day_trip_counts_every_one_of_its_days(self):
        conn = _conn(self)
        _viaje(conn, 1, "v1", "2026-03-10", "2026-03-14")
        conn.commit()
        dias = viajes.dias_de_viaje(conn, 1, "2026-03-01", "2026-03-31")
        self.assertEqual(dias, {"2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13", "2026-03-14"})

    def test_a_1_day_trip_is_ignored(self):
        conn = _conn(self)
        _viaje(conn, 1, "v1", "2026-03-10", "2026-03-10")
        conn.commit()
        dias = viajes.dias_de_viaje(conn, 1, "2026-03-01", "2026-03-31")
        self.assertEqual(dias, set())

    def test_a_trip_straddling_the_asked_range_is_clipped_to_it(self):
        conn = _conn(self)
        _viaje(conn, 1, "v1", "2026-03-28", "2026-04-03")  # straddles a month/season edge
        conn.commit()
        dias = viajes.dias_de_viaje(conn, 1, "2026-03-01", "2026-03-31")
        self.assertEqual(dias, {"2026-03-28", "2026-03-29", "2026-03-30", "2026-03-31"})
        dias_abril = viajes.dias_de_viaje(conn, 1, "2026-04-01", "2026-04-30")
        self.assertEqual(dias_abril, {"2026-04-01", "2026-04-02", "2026-04-03"})

    def test_another_person_s_trips_are_never_counted(self):
        conn = _conn(self)
        _viaje(conn, 2, "v1", "2026-03-10", "2026-03-14")
        conn.commit()
        dias = viajes.dias_de_viaje(conn, 1, "2026-03-01", "2026-03-31")
        self.assertEqual(dias, set())


if __name__ == "__main__":
    unittest.main()
