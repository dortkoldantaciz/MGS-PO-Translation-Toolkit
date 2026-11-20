from __future__ import annotations

import struct
from dataclasses import dataclass

from common import ToolkitError


STRING_RESOURCE_FLAG = 0x80
MAX_RESOURCE_DATA_SIZE = 0xFFFFFF


@dataclass
class GcxResource:
    index: int
    table_value: int
    data: bytes

    @property
    def kind_flag(self) -> int:
        return self.table_value >> 24

    @property
    def is_string(self) -> bool:
        return self.kind_flag == STRING_RESOURCE_FLAG


@dataclass
class GcxFile:
    prefix: bytes
    proc_offset: int
    resource_table_offset: int
    string_resources_offset: int
    font_data_offset: int
    seed: int
    resources: list[GcxResource]
    font_data: bytes
    script_data: bytes


def _decode_resource_data(data: bytes, seed: int) -> bytes:
    if seed == 0:
        return data
    out = bytearray(data)
    state = seed
    for index in range(len(out)):
        state = (state * 0x7D2B89DD + 0xCF9) & 0xFFFFFFFF
        out[index] ^= (state >> 15) & 0xFF
    return bytes(out)


def parse_gcx(data: bytes) -> GcxFile:
    if len(data) < 28:
        raise ToolkitError("GCX/FCX file is too small.")
    position = 4
    while True:
        if position + 4 > len(data):
            raise ToolkitError("GCX procedure table is not terminated.")
        value = struct.unpack_from("<i", data, position)[0]
        position += 4
        if value == -1:
            break
    block = position
    if block + 20 > len(data):
        raise ToolkitError("GCX block header is truncated.")
    proc_offset, table_offset, resources_offset, font_offset, seed = struct.unpack_from(
        "<IIIII", data, block
    )
    if not (20 <= table_offset <= resources_offset <= font_offset <= proc_offset):
        raise ToolkitError("GCX block offsets are invalid.")
    if block + proc_offset > len(data):
        raise ToolkitError("GCX procedure data points outside the file.")
    table_size = resources_offset - table_offset
    if table_size % 4:
        raise ToolkitError("GCX resource table is not aligned to 4 bytes.")
    count = table_size // 4
    table_start = block + table_offset
    resources_start = block + resources_offset
    resource_data_size = font_offset - resources_offset
    table_values = [struct.unpack_from("<I", data, table_start + index * 4)[0] for index in range(count)]
    decoded_blob = _decode_resource_data(bytes(data[resources_start:resources_start + resource_data_size]), seed)
    resources: list[GcxResource] = []
    for index, table_value in enumerate(table_values):
        start = table_value & MAX_RESOURCE_DATA_SIZE
        end = (
            table_values[index + 1] & MAX_RESOURCE_DATA_SIZE
            if index + 1 < count
            else resource_data_size
        )
        if start > end or end > resource_data_size:
            raise ToolkitError(f"GCX resource {index} has invalid boundaries.")
        resources.append(GcxResource(index, table_value, decoded_blob[start:end]))
    return GcxFile(
        prefix=bytes(data[:block]),
        proc_offset=proc_offset,
        resource_table_offset=table_offset,
        string_resources_offset=resources_offset,
        font_data_offset=font_offset,
        seed=seed,
        resources=resources,
        font_data=bytes(data[block + font_offset:block + proc_offset]),
        script_data=bytes(data[block + proc_offset:]),
    )


def build_gcx(
    parsed: GcxFile,
    replacements: dict[int, bytes] | None = None,
    *,
    prefix: bytes | None = None,
    font_data: bytes | None = None,
    script_data: bytes | None = None,
) -> bytes:
    replacements = replacements or {}
    prefix = parsed.prefix if prefix is None else prefix
    font_data = parsed.font_data if font_data is None else font_data
    script_data = parsed.script_data if script_data is None else script_data
    resource_blob = bytearray()
    table_values: list[int] = []
    for resource in parsed.resources:
        if len(resource_blob) > MAX_RESOURCE_DATA_SIZE:
            raise ToolkitError("GCX resource data exceeds its 24-bit offset limit.")
        table_values.append((resource.table_value & 0xFF000000) | len(resource_blob))
        resource_blob += replacements.get(resource.index, resource.data)
    if len(resource_blob) > MAX_RESOURCE_DATA_SIZE:
        raise ToolkitError("GCX resource data exceeds its 24-bit offset limit.")
    # The reference builder aligns the extra/font block after changing text.
    # A no-change roundtrip cannot expose this: retail pools already contain
    # their padding. Growing by 1..3 bytes otherwise misaligns embedded data.
    if replacements:
        resource_blob += bytes((-(parsed.string_resources_offset + len(resource_blob))) % 4)
    font_offset = parsed.string_resources_offset + len(resource_blob)
    proc_offset = font_offset + len(font_data)
    out = bytearray(prefix)
    out += struct.pack(
        "<IIIII",
        proc_offset,
        parsed.resource_table_offset,
        parsed.string_resources_offset,
        font_offset,
        parsed.seed,
    )
    for value in table_values:
        out += struct.pack("<I", value)
    expected_resources_start = len(prefix) + parsed.string_resources_offset
    if len(out) != expected_resources_start:
        raise ToolkitError("GCX layout contains an unsupported gap before resource data.")
    out += _decode_resource_data(bytes(resource_blob), parsed.seed)
    out += font_data
    out += script_data
    return bytes(out)
