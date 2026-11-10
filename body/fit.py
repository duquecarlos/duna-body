"""BODY-GARMIN-IMPORT-1: a minimal FIT (Garmin's binary activity-file format) reader, ours, no
dependency. Decodes only what body.importar_garmin needs: file_id's `type` (global msg 0, field
0), monitoring's `heart_rate`/`timestamp_16`/`timestamp` (global msg 55, fields 27/26/253), and
stress_level's `stress_level_value`/`stress_level_time` (global msg 227, fields 0/1). Any other
field in a record is skipped by its declared byte length, never decoded -- the definition message
states each field's size directly, so skipping needs no base-type knowledge at all.

CRC is NOT checked: the trailing 2-byte file CRC and an optional 2-byte header CRC (header_size
14) are both read past rather than verified. The fields we decode either parse correctly or raise
FitError on a structurally broken stream (bad header, truncated record, a data message for an
undefined local type) -- a flipped CRC bit with an otherwise well-formed stream would still read
correctly, which is the behaviour wanted here (we only need the fields we ask for).

Record headers, both forms, and definition messages in both byte orders are handled. Developer
field definitions (record header bit 0x20 on a definition message) are read and skipped -- we
never decode a developer field, only consume its declared bytes so the stream position stays
correct for what follows.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field as _field
from typing import Iterable, Iterator

FIT_EPOCH_UNIX_S = 631065600  # 1989-12-31T00:00:00Z, seconds since the Unix epoch


class FitError(Exception):
    """A corrupt or truncated FIT stream -- caught per file by the caller, never a crash of the
    whole import run."""


# global msg num 55 ("monitoring"), field 26: a 16-bit timestamp that rolls forward from the last
# full (field 253) timestamp -- the only field that needs this special reconstruction.
_MSG_MONITORING = 55
_FIELD_TIMESTAMP_16 = 26
_FIELD_TIMESTAMP = 253

# type_num (base_type byte & 0x1F) -> (size_bytes, signed). Only the integer widths this reader's
# five target fields actually use (uint8/16/32 and their signed counterparts, the latter needed
# for stress_level_value's negative "no reading" sentinels) -- see module docstring.
_TYPE_INFO = {
    0: (1, False),   # enum
    1: (1, True),    # sint8
    2: (1, False),   # uint8
    3: (2, True),    # sint16
    4: (2, False),   # uint16
    5: (4, True),    # sint32
    6: (4, False),   # uint32
}
_STRUCT_CODE = {(1, True): "b", (1, False): "B", (2, True): "h", (2, False): "H",
                (4, True): "i", (4, False): "I"}


def _decode_int(raw: bytes, base_type: int, little_endian: bool) -> int | None:
    info = _TYPE_INFO.get(base_type & 0x1F)
    if info is None:
        return None
    size, signed = info
    if len(raw) != size:
        return None  # an array field, not the scalar our target fields expect -- an honest gap
    code = _STRUCT_CODE[(size, signed)]
    value = struct.unpack(("<" if little_endian else ">") + code, raw)[0]
    invalid = (1 << (8 * size - (1 if signed else 0))) - 1
    return None if value == invalid else value


@dataclass
class _FieldDef:
    field_num: int
    size: int
    base_type: int  # -1 marks a developer field: never decoded, only skipped


@dataclass
class _MsgDef:
    global_msg_num: int
    little_endian: bool
    fields: list[_FieldDef] = _field(default_factory=list)
    total_size: int = 0


def _read_definition(data: bytes, pos: int, record_header: int, end: int) -> tuple[int, _MsgDef]:
    if pos + 5 > end:
        raise FitError("truncated definition message")
    architecture = data[pos + 1]
    little_endian = architecture == 0
    order = "<" if little_endian else ">"
    global_msg_num = struct.unpack(order + "H", data[pos + 2:pos + 4])[0]
    num_fields = data[pos + 4]
    pos += 5
    fields: list[_FieldDef] = []
    for _ in range(num_fields):
        if pos + 3 > end:
            raise FitError("truncated field definition")
        fields.append(_FieldDef(data[pos], data[pos + 1], data[pos + 2]))
        pos += 3
    if record_header & 0x20:  # developer fields follow
        if pos >= end:
            raise FitError("truncated developer field count")
        num_dev = data[pos]
        pos += 1
        for _ in range(num_dev):
            if pos + 3 > end:
                raise FitError("truncated developer field definition")
            fields.append(_FieldDef(data[pos], data[pos + 1], -1))
            pos += 3
    total_size = sum(f.size for f in fields)
    return pos, _MsgDef(global_msg_num, little_endian, fields, total_size)


def _read_data_fields(data: bytes, pos: int, msg_def: _MsgDef, end: int,
                       wanted_fields: frozenset[int]) -> tuple[int, dict[int, int]]:
    if pos + msg_def.total_size > end:
        raise FitError("truncated data message")
    out: dict[int, int] = {}
    for f in msg_def.fields:
        raw = data[pos:pos + f.size]
        pos += f.size
        if f.base_type == -1:
            continue
        if f.field_num not in wanted_fields:
            continue
        value = _decode_int(raw, f.base_type, msg_def.little_endian)
        if value is not None:
            out[f.field_num] = value
    return pos, out


def read_messages(data: bytes, wanted: dict[int, set[int]]) -> Iterator[tuple[int, dict[int, int]]]:
    """Yields (global_msg_num, {field_num: value}) for every data message whose global_msg_num is
    a key of `wanted` (whose value names the field numbers to decode for that message type).
    `_FIELD_TIMESTAMP` (253) is always decoded in addition, for every message type encountered --
    whether or not the caller asked for it -- so the running timestamp used to reconstruct a
    compressed header's offset or monitoring's 16-bit field stays correct even across message
    types the caller never asked about. A yielded dict carries 253 too, filled in from that
    running timestamp when the record did not carry its own."""
    if len(data) < 12:
        raise FitError("too short to be a FIT file")
    header_size = data[0]
    if header_size not in (12, 14):
        raise FitError(f"unexpected header size {header_size}")
    if len(data) < header_size or data[8:12] != b".FIT":
        raise FitError("missing .FIT signature")
    data_size = struct.unpack("<I", data[4:8])[0]
    pos = header_size
    end = header_size + data_size
    if end > len(data):
        raise FitError("truncated file (data_size exceeds file length)")

    local_defs: dict[int, _MsgDef] = {}
    last_timestamp: int | None = None

    while pos < end:
        record_header = data[pos]
        pos += 1
        if record_header & 0x80:  # compressed timestamp header
            local_type = (record_header >> 5) & 0x03
            offset = record_header & 0x1F
            msg_def = local_defs.get(local_type)
            if msg_def is None:
                raise FitError(f"compressed-header data message for undefined local type {local_type}")
            wanted_fields = wanted.get(msg_def.global_msg_num, frozenset()) | {_FIELD_TIMESTAMP}
            pos, fields = _read_data_fields(data, pos, msg_def, end, frozenset(wanted_fields))
            if last_timestamp is None:
                raise FitError("compressed timestamp header before any full timestamp")
            new_ts = (last_timestamp & ~0x1F) | offset
            if new_ts < last_timestamp:
                new_ts += 0x20
            last_timestamp = new_ts
            fields[_FIELD_TIMESTAMP] = last_timestamp
            if msg_def.global_msg_num in wanted:
                yield msg_def.global_msg_num, fields
            continue

        is_definition = bool(record_header & 0x40)
        local_type = record_header & 0x0F
        if is_definition:
            pos, msg_def = _read_definition(data, pos, record_header, end)
            local_defs[local_type] = msg_def
            continue

        msg_def = local_defs.get(local_type)
        if msg_def is None:
            raise FitError(f"data message for undefined local type {local_type}")
        wanted_fields = wanted.get(msg_def.global_msg_num, frozenset()) | {_FIELD_TIMESTAMP}
        if msg_def.global_msg_num == _MSG_MONITORING:
            wanted_fields = wanted_fields | {_FIELD_TIMESTAMP_16}
        pos, fields = _read_data_fields(data, pos, msg_def, end, frozenset(wanted_fields))

        if _FIELD_TIMESTAMP in fields:
            last_timestamp = fields[_FIELD_TIMESTAMP]
        elif (msg_def.global_msg_num == _MSG_MONITORING and _FIELD_TIMESTAMP_16 in fields
              and last_timestamp is not None):
            ts16 = fields[_FIELD_TIMESTAMP_16]
            new_ts = (last_timestamp & ~0xFFFF) | ts16
            if new_ts < last_timestamp:
                new_ts += 0x10000
            last_timestamp = new_ts
            fields[_FIELD_TIMESTAMP] = last_timestamp
        elif last_timestamp is not None:
            fields[_FIELD_TIMESTAMP] = last_timestamp

        if msg_def.global_msg_num in wanted:
            yield msg_def.global_msg_num, fields
