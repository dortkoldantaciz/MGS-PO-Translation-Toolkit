from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from common import ToolkitError, align


@dataclass
class NamedEntry:
    name: str
    data: bytes
    hash_value: int | None = None


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ToolkitError("Archive table points outside the file.")
    return struct.unpack_from("<I", data, offset)[0]


def parse_zar(data: bytes) -> list[NamedEntry]:
    if len(data) < 6:
        raise ToolkitError("ZAR file is too small.")
    expected_size = _u32(data, 0)
    try:
        unpacked = zlib.decompress(data[4:])
    except zlib.error as exc:
        raise ToolkitError(f"Invalid ZAR compression stream: {exc}") from exc
    if len(unpacked) != expected_size:
        raise ToolkitError(
            f"ZAR size mismatch: header says {expected_size}, decoded {len(unpacked)}."
        )
    return parse_dar(unpacked)


def build_zar(entries: list[NamedEntry], compression_level: int = 9) -> bytes:
    unpacked = build_dar(entries)
    return struct.pack("<I", len(unpacked)) + zlib.compress(unpacked, compression_level)


def parse_dar(data: bytes) -> list[NamedEntry]:
    count = _u32(data, 0)
    if count > 1_000_000:
        raise ToolkitError(f"Invalid DAR entry count: {count}")
    position = 4
    entries: list[NamedEntry] = []
    for _index in range(count):
        try:
            name_end = data.index(0, position)
        except ValueError as exc:
            raise ToolkitError("DAR entry name is not terminated.") from exc
        try:
            name = data[position:name_end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ToolkitError("DAR entry name is not ASCII.") from exc
        position = align(name_end + 1, 4)
        size = _u32(data, position)
        position = align(position + 4, 16)
        end = position + size
        if end > len(data):
            raise ToolkitError(f"DAR entry {name!r} extends past the archive.")
        entries.append(NamedEntry(name=name, data=bytes(data[position:end])))
        position = end + 1
    return entries


def build_dar(entries: list[NamedEntry]) -> bytes:
    out = bytearray(struct.pack("<I", len(entries)))
    for entry in entries:
        try:
            encoded_name = entry.name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ToolkitError(f"DAR entry name is not ASCII: {entry.name!r}") from exc
        out += encoded_name + b"\0"
        out += b"\0" * (align(len(out), 4) - len(out))
        out += struct.pack("<I", len(entry.data))
        out += b"\0" * (align(len(out), 16) - len(out))
        out += entry.data
        out += b"\0"
    return bytes(out)


def parse_qar(data: bytes) -> list[NamedEntry]:
    if len(data) < 8:
        raise ToolkitError("QAR file is too small.")
    table_offset = _u32(data, len(data) - 4)
    if table_offset >= len(data) - 4:
        raise ToolkitError("QAR table offset is outside the file.")
    count = _u32(data, table_offset)
    if count > 1_000_000:
        raise ToolkitError(f"Invalid QAR entry count: {count}")
    table_position = table_offset + 4
    headers: list[tuple[int, int]] = []
    for index in range(count):
        offset = table_position + index * 8
        if offset + 8 > len(data) - 4:
            raise ToolkitError("QAR entry table is truncated.")
        headers.append(struct.unpack_from("<II", data, offset))
    name_position = table_position + count * 8
    names: list[str] = []
    for _index in range(count):
        try:
            name_end = data.index(0, name_position)
        except ValueError as exc:
            raise ToolkitError("QAR entry name is not terminated.") from exc
        try:
            names.append(data[name_position:name_end].decode("ascii"))
        except UnicodeDecodeError as exc:
            raise ToolkitError("QAR entry name is not ASCII.") from exc
        name_position = name_end + 1
    data_position = 0
    entries: list[NamedEntry] = []
    for name, (hash_value, size) in zip(names, headers):
        end = data_position + size
        if end > table_offset:
            raise ToolkitError(f"QAR entry {name!r} overlaps the file table.")
        entries.append(NamedEntry(name=name, data=bytes(data[data_position:end]), hash_value=hash_value))
        data_position = align(end, 0x80)
    return entries


def build_qar(entries: list[NamedEntry]) -> bytes:
    out = bytearray()
    for entry in entries:
        out += entry.data
        out += b"\0" * (align(len(out), 0x80) - len(out))
    table_offset = len(out)
    out += struct.pack("<I", len(entries))
    for entry in entries:
        if entry.hash_value is None:
            raise ToolkitError(f"QAR entry {entry.name!r} has no stored hash.")
        out += struct.pack("<II", entry.hash_value, len(entry.data))
    for entry in entries:
        try:
            out += entry.name.encode("ascii") + b"\0"
        except UnicodeEncodeError as exc:
            raise ToolkitError(f"QAR entry name is not ASCII: {entry.name!r}") from exc
    out += b"\0" * (align(len(out), 4) - len(out))
    out += struct.pack("<I", table_offset)
    return bytes(out)
