"""BODY-DESPERTAR-1: the wake check Personal's desk watches for -- one sleep-only Garmin call per
run (roughly every 20 minutes, 04:00-13:00), writing BODY_HOME/vida/despertar-<day>.json as soon
as last night's wake time is known, and none at all once that file exists or the daily pull
already stored it. Every Garmin call is mocked at the adapter boundary -- NO NETWORK.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, despertar, garmin_adapter


def _vida_path(day: str):
    return config.BODY_HOME / "vida" / f"despertar-{day}.json"


class _AisladoTestCase(unittest.TestCase):
    """Points despertar at its OWN temporary BODY_HOME (despertar.py reads config.BODY_HOME fresh on
    every call, so patching the attribute here is enough) -- never the shared one tests/test_dispositivo.py
    asserts is absent."""
    def setUp(self):
        patcher = mock.patch.object(config, "BODY_HOME", Path(tempfile.mkdtemp(dir=TMP)))
        patcher.start()
        self.addCleanup(patcher.stop)


class FileAlreadyExistsTest(_AisladoTestCase):
    def test_zero_calls_when_file_exists(self):
        day = "2026-01-10"
        path = _vida_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"woke_at": "x", "source": "garmin", "written_at": "y"}), encoding="utf-8",
        )

        with mock.patch.object(garmin_adapter, "fetch_sleep") as fetch, \
                mock.patch.object(garmin_adapter, "has_token") as has_token, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        self.assertIn("ya escrito", out.getvalue())
        fetch.assert_not_called()
        has_token.assert_not_called()


class SleepAlreadyStoredTest(_AisladoTestCase):
    def test_zero_calls_and_file_written_from_the_database(self):
        day = "2026-01-11"
        path = _vida_path(day)

        conn = db.connect()
        try:
            db.init(conn)
            person_id = db.local_person_id(conn)
            conn.execute(
                "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
                (person_id, day, f"{day}T23:00", "2026-01-12T06:10"),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(garmin_adapter, "fetch_sleep") as fetch, \
                mock.patch.object(garmin_adapter, "has_token") as has_token, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        self.assertIn("ya en la base", out.getvalue())
        fetch.assert_not_called()
        has_token.assert_not_called()
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"woke_at", "source", "written_at"})
        self.assertEqual(data["source"], "garmin")
        self.assertEqual(data["woke_at"], "2026-01-12T06:10-05:00")  # America/Bogota: fixed -05:00
        self.assertTrue(data["written_at"].endswith("-05:00"))


class NoTokenTest(_AisladoTestCase):
    def test_exit_0_no_file_no_call_when_no_garmin_session(self):
        day = "2026-01-12"
        path = _vida_path(day)

        with mock.patch.object(garmin_adapter, "has_token", return_value=False), \
                mock.patch.object(garmin_adapter, "fetch_sleep") as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        self.assertIn("sin sesion de Garmin", out.getvalue())
        fetch.assert_not_called()
        self.assertFalse(path.exists())


class LockHeldTest(_AisladoTestCase):
    def test_skips_the_check_when_a_pull_holds_the_lock(self):
        day = "2026-01-13"
        path = _vida_path(day)

        with mock.patch.object(garmin_adapter, "has_token", return_value=True), \
                mock.patch.object(despertar.pull, "_try_lock", return_value=None), \
                mock.patch.object(garmin_adapter, "fetch_sleep") as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        self.assertIn("pull en curso", out.getvalue())
        fetch.assert_not_called()
        self.assertFalse(path.exists())


class FetchReturnsWakeTest(_AisladoTestCase):
    def test_file_written_with_exactly_three_keys(self):
        day = "2026-01-14"
        path = _vida_path(day)

        with mock.patch.object(garmin_adapter, "has_token", return_value=True), \
                mock.patch.object(despertar.pull, "_try_lock", return_value=object()), \
                mock.patch.object(despertar.pull, "_unlock") as unlock, \
                mock.patch.object(garmin_adapter, "fetch_sleep", return_value=f"{day}T06:20") as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        self.assertIn("escrito", out.getvalue())
        fetch.assert_called_once_with(day)
        unlock.assert_called_once()
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"woke_at", "source", "written_at"})
        self.assertEqual(data["source"], "garmin")
        self.assertEqual(data["woke_at"], f"{day}T06:20-05:00")
        self.assertTrue(data["written_at"].endswith("-05:00"))


class FetchReturnsNoneYetTest(_AisladoTestCase):
    def test_no_file_when_last_night_has_not_synced_yet(self):
        day = "2026-01-15"
        path = _vida_path(day)

        with mock.patch.object(garmin_adapter, "has_token", return_value=True), \
                mock.patch.object(despertar.pull, "_try_lock", return_value=object()), \
                mock.patch.object(despertar.pull, "_unlock"), \
                mock.patch.object(garmin_adapter, "fetch_sleep", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        self.assertIn("sin dato todavia", out.getvalue())
        self.assertFalse(path.exists())


class FetchFailsTest(_AisladoTestCase):
    def test_error_kind_only_no_payload_exit_0(self):
        day = "2026-01-16"
        path = _vida_path(day)

        with mock.patch.object(garmin_adapter, "has_token", return_value=True), \
                mock.patch.object(despertar.pull, "_try_lock", return_value=object()), \
                mock.patch.object(despertar.pull, "_unlock") as unlock, \
                mock.patch.object(garmin_adapter, "fetch_sleep",
                                  side_effect=RuntimeError("a health figure that must never print")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main(["--day", day])

        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn("error RuntimeError", printed)
        self.assertNotIn("health figure", printed)
        self.assertFalse(path.exists())
        unlock.assert_called_once()


class HostedTest(unittest.TestCase):
    def test_hosted_refuses_and_writes_nothing(self):
        with mock.patch.object(despertar.cuenta, "hosted", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = despertar.main([])

        self.assertEqual(code, 0)
        self.assertIn("hosted", out.getvalue())


if __name__ == "__main__":
    unittest.main()
