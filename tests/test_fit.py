"""body/fit.py's own round-trip: header, normal and compressed-timestamp record headers,
definition messages, developer fields skipped, invalid-value sentinels, and timestamp
reconstruction (a full 253 sets the base; a monitoring timestamp_16 and a compressed header's
offset both roll forward from it). No real FIT file, no real health figure -- every byte here is
hand-built by tests/_fit_fixtures.py."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests import _fit_fixtures as ff
from body import fit


def _collect(data: bytes, wanted: dict[int, set[int]]) -> list[tuple[int, dict[int, int]]]:
    return list(fit.read_messages(data, wanted))


class HeaderTest(unittest.TestCase):
    def test_too_short_is_an_error(self):
        with self.assertRaises(fit.FitError):
            _collect(b"short", {0: {0}})

    def test_bad_signature_is_an_error(self):
        bad = ff.header(0)[:8] + b"XXXX"
        with self.assertRaises(fit.FitError):
            _collect(bad + b"\x00\x00", {0: {0}})

    def test_truncated_data_size_is_an_error(self):
        good = ff.fit_file(ff.file_id_record(0, ff.TYPE_MONITORING_B))
        truncated = good[:-10]  # claims more data than is actually present
        with self.assertRaises(fit.FitError):
            _collect(truncated, {0: {0}})


class FileIdTest(unittest.TestCase):
    def test_reads_the_file_type_enum(self):
        data = ff.fit_file(ff.file_id_record(0, ff.TYPE_MONITORING_B))
        out = _collect(data, {0: {0}})
        self.assertEqual(len(out), 1)
        gmn, fields = out[0]
        self.assertEqual(gmn, 0)
        self.assertEqual(fields[0], ff.TYPE_MONITORING_B)


class MonitoringTimestampTest(unittest.TestCase):
    def test_full_timestamp_sets_the_base_and_16bit_rolls_forward(self):
        records = []
        records += ff.file_id_record(0, ff.TYPE_MONITORING_B)
        base_unix = 1_700_000_000
        records.append(ff.definition(1, ff.MSG_MONITORING,
                                      [(ff.FIELD_TIMESTAMP, 4, ff.BASE_UINT32), (ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        records.append(ff.data(1, ff.u32(ff.fit_ts(base_unix)) + ff.u8(60)))
        records.append(ff.definition(2, ff.MSG_MONITORING,
                                      [(ff.FIELD_MONITORING_TS16, 2, ff.BASE_UINT16), (ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        ts16_60s_later = (ff.fit_ts(base_unix) + 60) & 0xFFFF
        records.append(ff.data(2, ff.u16(ts16_60s_later) + ff.u8(61)))
        data = ff.fit_file(records)
        out = _collect(data, {55: {26, 27, 253}})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1][253], ff.fit_ts(base_unix))
        self.assertEqual(out[0][1][27], 60)
        self.assertEqual(out[1][1][253], ff.fit_ts(base_unix) + 60)
        self.assertEqual(out[1][1][27], 61)

    def test_16bit_timestamp_rolls_over_past_65536(self):
        records = []
        # A full timestamp whose low 16 bits sit near the top of the 16-bit range, so the next
        # record's own timestamp_16 (32 seconds later) wraps past 0xFFFF -- exactly the rollover
        # read_messages must correct for.
        t0 = 1_000_000_000 - (1_000_000_000 % 0x10000) + 0xFFF0
        records.append(ff.definition(1, ff.MSG_MONITORING,
                                      [(ff.FIELD_TIMESTAMP, 4, ff.BASE_UINT32), (ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        records.append(ff.data(1, ff.u32(t0) + ff.u8(60)))
        records.append(ff.definition(2, ff.MSG_MONITORING,
                                      [(ff.FIELD_MONITORING_TS16, 2, ff.BASE_UINT16), (ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        t1 = t0 + 32
        records.append(ff.data(2, ff.u16(t1 & 0xFFFF) + ff.u8(61)))
        data = ff.fit_file(records)
        out = _collect(data, {55: {26, 27, 253}})
        self.assertEqual(out[1][1][253], t1)

    def test_compressed_header_offset_rolls_forward(self):
        records = []
        base_unix = 1_700_000_000
        records.append(ff.definition(1, ff.MSG_MONITORING, [(ff.FIELD_TIMESTAMP, 4, ff.BASE_UINT32),
                                                             (ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        records.append(ff.data(1, ff.u32(ff.fit_ts(base_unix)) + ff.u8(60)))
        # Redefine local type 1 with ONLY heart_rate -- the compressed header carries the time.
        records.append(ff.definition(1, ff.MSG_MONITORING, [(ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        records.append(ff.data_compressed(1, 5, ff.u8(62)))  # +5s
        data = ff.fit_file(records)
        out = _collect(data, {55: {27, 253}})
        self.assertEqual(out[1][1][253], ff.fit_ts(base_unix) + 5)
        self.assertEqual(out[1][1][27], 62)

    def test_invalid_heart_rate_byte_is_dropped(self):
        records = []
        base_unix = 1_700_000_000
        records.append(ff.definition(1, ff.MSG_MONITORING, [(ff.FIELD_TIMESTAMP, 4, ff.BASE_UINT32),
                                                             (ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8)]))
        records.append(ff.data(1, ff.u32(ff.fit_ts(base_unix)) + ff.u8(0xFF)))  # 0xFF = invalid uint8
        data = ff.fit_file(records)
        out = _collect(data, {55: {27, 253}})
        self.assertNotIn(27, out[0][1])


class StressTest(unittest.TestCase):
    def test_reads_value_and_time(self):
        records = []
        base_unix = 1_700_000_000
        records.append(ff.definition(3, ff.MSG_STRESS_LEVEL, [(ff.FIELD_STRESS_TIME, 4, ff.BASE_UINT32),
                                                              (ff.FIELD_STRESS_VALUE, 2, ff.BASE_SINT16)]))
        records.append(ff.data(3, ff.u32(ff.fit_ts(base_unix)) + ff.s16(35)))
        data = ff.fit_file(records)
        out = _collect(data, {227: {0, 1}})
        self.assertEqual(out[0][1][1], ff.fit_ts(base_unix))
        self.assertEqual(out[0][1][0], 35)

    def test_negative_sentinel_decodes_as_a_plain_negative_number(self):
        """FIT's own invalid sentinel for sint16 is 0x7FFF; -1/-2 decode fine at this layer --
        importar_garmin's own 'negative = no reading' rule drops them, not this reader."""
        records = []
        records.append(ff.definition(3, ff.MSG_STRESS_LEVEL, [(ff.FIELD_STRESS_TIME, 4, ff.BASE_UINT32),
                                                              (ff.FIELD_STRESS_VALUE, 2, ff.BASE_SINT16)]))
        records.append(ff.data(3, ff.u32(ff.fit_ts(1_700_000_000)) + ff.s16(-1)))
        data = ff.fit_file(records)
        out = _collect(data, {227: {0, 1}})
        self.assertEqual(out[0][1][0], -1)


class DeveloperFieldTest(unittest.TestCase):
    def test_developer_fields_are_skipped_without_breaking_the_stream(self):
        rh = 0x40 | 0x20 | 1  # definition message, local type 1, developer-data bit set
        body = bytes([0, 0]) + ff.u16(ff.MSG_MONITORING) + bytes([1]) \
            + bytes([ff.FIELD_MONITORING_HR, 1, ff.BASE_UINT8]) \
            + bytes([1]) + bytes([9, 2, 0])  # one dev field: num=9, size=2, dev_index=0
        definition = bytes([rh]) + body
        data_msg = ff.data(1, ff.u8(60) + b"\x11\x22")  # hr byte + 2 dev-field bytes to skip
        data = ff.fit_file([definition, data_msg])
        out = _collect(data, {55: {27}})
        self.assertEqual(out[0][1][27], 60)


class UndefinedLocalTypeTest(unittest.TestCase):
    def test_data_message_for_an_undefined_local_type_is_an_error(self):
        data = ff.fit_file([ff.data(1, ff.u8(60))])
        with self.assertRaises(fit.FitError):
            _collect(data, {55: {27}})


if __name__ == "__main__":
    unittest.main()
