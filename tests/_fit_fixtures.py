"""Import tests/_isolate.py FIRST in any test module using these -- they are plain byte builders
with no I/O of their own, but every test module that imports `body` must isolate first.

Tiny, hand-built, valid FIT files for body.fit/body.importar_garmin's tests: no FIT SDK, no
third-party dependency -- the same minimal record/definition encoding body/fit.py decodes, built
by hand so the tests exercise the real byte format, not a mock of it. All values are invented
demo numbers, never anything from a real device or account.
"""
from __future__ import annotations

import struct
import zipfile
from io import BytesIO
from pathlib import Path

from body.fit import FIT_EPOCH_UNIX_S

BASE_ENUM = 0x00
BASE_UINT8 = 0x02
BASE_SINT16 = 0x83
BASE_UINT16 = 0x84
BASE_UINT32 = 0x86

MSG_FILE_ID = 0
MSG_MONITORING = 55
MSG_STRESS_LEVEL = 227

FIELD_FILE_TYPE = 0
FIELD_STRESS_VALUE = 0
FIELD_STRESS_TIME = 1
FIELD_MONITORING_TS16 = 26
FIELD_MONITORING_HR = 27
FIELD_TIMESTAMP = 253

TYPE_MONITORING_B = 32
TYPE_ACTIVITY = 4


def fit_ts(unix_s: int) -> int:
    """A FIT-epoch timestamp (seconds since 1989-12-31T00:00:00Z) for a given Unix timestamp."""
    return unix_s - FIT_EPOCH_UNIX_S


def header(data_size: int) -> bytes:
    return struct.pack("<BBHI4s", 12, 16, 100, data_size, b".FIT")


def definition(local_type: int, global_msg_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    """fields: list of (field_num, size, base_type), little-endian architecture."""
    rh = 0x40 | local_type
    body = struct.pack("<BB", 0, 0) + struct.pack("<H", global_msg_num) + struct.pack("<B", len(fields))
    for field_num, size, base_type in fields:
        body += struct.pack("<BBB", field_num, size, base_type)
    return bytes([rh]) + body


def data(local_type: int, raw_fields: bytes) -> bytes:
    """raw_fields: the field values packed in the SAME order as the matching definition."""
    rh = 0x00 | local_type
    return bytes([rh]) + raw_fields


def data_compressed(local_type_0_3: int, offset_s: int, raw_fields: bytes) -> bytes:
    rh = 0x80 | ((local_type_0_3 & 0x03) << 5) | (offset_s & 0x1F)
    return bytes([rh]) + raw_fields


def u8(v: int) -> bytes:
    return struct.pack("<B", v)


def u16(v: int) -> bytes:
    return struct.pack("<H", v)


def u32(v: int) -> bytes:
    return struct.pack("<I", v)


def s16(v: int) -> bytes:
    return struct.pack("<h", v)


def fit_file(records: list[bytes]) -> bytes:
    """Assembles a complete FIT file: header, records, a 2-byte footer stand-in for the CRC this
    reader never checks (any bytes would do; these are not a real CRC)."""
    body = b"".join(records)
    return header(len(body)) + body + b"\x00\x00"


def file_id_record(local_type: int, file_type: int) -> list[bytes]:
    return [
        definition(local_type, MSG_FILE_ID, [(FIELD_FILE_TYPE, 1, BASE_ENUM)]),
        data(local_type, u8(file_type)),
    ]


def monitoring_b_fit_file(hr_samples: list[tuple[int, int]] = (),
                           stress_samples: list[tuple[int, int]] = ()) -> bytes:
    """A monitoring_b (file_id.type=32) FIT file: one full-timestamp monitoring record per
    (unix_s, bpm) in hr_samples, one full-timestamp stress_level record per (unix_s, value) in
    stress_samples -- simple, independent records (no 16-bit/compressed timestamps here; those
    are covered directly in tests/test_fit.py)."""
    records = list(file_id_record(0, TYPE_MONITORING_B))
    for unix_s, bpm in hr_samples:
        records.append(definition(1, MSG_MONITORING, [(FIELD_TIMESTAMP, 4, BASE_UINT32),
                                                        (FIELD_MONITORING_HR, 1, BASE_UINT8)]))
        records.append(data(1, u32(fit_ts(unix_s)) + u8(bpm)))
    for unix_s, value in stress_samples:
        records.append(definition(2, MSG_STRESS_LEVEL, [(FIELD_STRESS_TIME, 4, BASE_UINT32),
                                                          (FIELD_STRESS_VALUE, 2, BASE_SINT16)]))
        records.append(data(2, u32(fit_ts(unix_s)) + s16(value)))
    return fit_file(records)


def activity_fit_file() -> bytes:
    """file_id.type=4 (activity) -- left alone by this slice's importer; no monitoring/stress."""
    return fit_file(file_id_record(0, TYPE_ACTIVITY))


def corrupt_fit_bytes() -> bytes:
    """Starts like a real FIT file (valid header, valid .FIT signature) but is truncated mid
    record -- a corrupt/truncated file, reported as an error rather than crashing the run."""
    good = monitoring_b_fit_file(hr_samples=[(1_700_000_000, 60)])
    return good[:-5]


def build_export_zip(dest: Path, inner_files: dict[str, bytes], profile_bytes: bytes = b'{"never":"read"}') -> None:
    """Writes an outer zip shaped like Carlos's Garmin export: one inner zip of .fit files under
    DI_CONNECT/DI-Connect-Uploaded-Files/, plus a sibling profile entry that must never be opened."""
    inner_buf = BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner:
        for name, content in inner_files.items():
            inner.writestr(name, content)
    with zipfile.ZipFile(dest, "w") as outer:
        outer.writestr("DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip", inner_buf.getvalue())
        outer.writestr("Profile/profile.json", profile_bytes)
