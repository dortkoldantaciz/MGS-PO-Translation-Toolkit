from __future__ import annotations

from dataclasses import dataclass
import struct

from common import ToolkitError


@dataclass(frozen=True)
class GclString:
    index: int
    data: bytes


@dataclass(frozen=True)
class GclScriptString:
    procedure_kind: str
    procedure_index: int
    string_index: int
    data: bytes


@dataclass(frozen=True)
class GclNode:
    kind: str
    value: int | str | None = None
    children: tuple["GclNode", ...] = ()
    integer_offset: int | None = None


PORTABLE_OPS_LANGUAGE_SELECTOR = bytes.fromhex(
    "8d806d16860d00113711800002c1aba0886698e38b0141000000"
    "6d16860d00113711800002c2aba0886698e38b0142000000"
    "6d16860d00113711800002c3aba0886698e38b0143000000"
    "6d16860d00113711800002c4aba0886698e38b0144000000"
    "6d16860d00113711800002c5aba0886698e38b0145000000"
    "6698e38b01410000"
)
PORTABLE_OPS_LOCALIZATION_COMMAND = 0xAB5A2A
PORTABLE_OPS_FONT_LOCALIZATION_COMMAND = 0xB7B5A7
PORTABLE_OPS_NAMING_LOCALIZATION_COMMAND = 0x231EA8


class _GclInspector:
    @staticmethod
    def _size(data: bytes, position: int, limit: int) -> tuple[int, int, int]:
        if position >= limit:
            raise ToolkitError("Truncated GCL block size.")
        first = data[position]
        code = first & 0x0F
        if code == 0x0D:
            if position + 2 > limit:
                raise ToolkitError("Truncated GCL block size.")
            return first & 0xF0, data[position + 1], 2
        if code == 0x0E:
            if position + 3 > limit:
                raise ToolkitError("Truncated GCL block size.")
            return first & 0xF0, int.from_bytes(data[position + 1:position + 3], "little"), 3
        return first & 0xF0, code, 1

    @staticmethod
    def _short_size(data: bytes, position: int, limit: int) -> tuple[int, int]:
        if position >= limit:
            raise ToolkitError("Truncated GCL command header.")
        first = data[position]
        if first <= 0x7F:
            return first, 1
        if position + 2 > limit:
            raise ToolkitError("Truncated GCL command header.")
        return ((first << 8) | data[position + 1]) & 0x7FFF, 2

    def stream(self, data: bytes, start: int, end: int) -> tuple[GclNode, ...]:
        nodes: list[GclNode] = []
        position = start
        while position < end:
            node, position = self.token(data, position, end)
            nodes.append(node)
        if position != end:
            raise ToolkitError("A GCL token crossed its parent block boundary.")
        return tuple(nodes)

    def token(self, data: bytes, position: int, limit: int) -> tuple[GclNode, int]:
        if position >= limit:
            raise ToolkitError("Unexpected end of GCL data.")
        first = data[position]
        tag = first & 0xF0

        if tag == 0:
            fixed = {
                0x00: 0, 0x01: 2, 0x02: 1, 0x03: 1, 0x04: 1,
                0x06: 3, 0x08: 2, 0x09: 4, 0x0A: 4, 0x0D: 4, 0x0E: 2,
            }
            if first == 0x07:
                if position + 2 > limit:
                    raise ToolkitError("Truncated inline GCL string.")
                length = data[position + 1]
                end = position + 2 + length
                if end > limit or length == 0 or data[end - 1] != 0:
                    raise ToolkitError("Invalid inline GCL string record.")
                return GclNode("string"), end
            if first not in fixed:
                raise ToolkitError(f"Unsupported GCL value type 0x{first:02X}.")
            end = position + 1 + fixed[first]
            if end > limit:
                raise ToolkitError("Truncated GCL value.")
            if first == 0x01:
                value = int.from_bytes(data[position + 1:end], "little", signed=True)
                return GclNode("integer", value), end
            if first in (0x02, 0x03, 0x04):
                return GclNode("integer", data[position + 1]), end
            if first == 0x08:
                return GclNode("integer", int.from_bytes(data[position + 1:end], "little")), end
            if first == 0x09:
                value = int.from_bytes(data[position + 1:end], "little", signed=True)
                return GclNode("integer", value, integer_offset=position + 1), end
            if first == 0x0A:
                return GclNode("integer", int.from_bytes(data[position + 1:end], "little"), integer_offset=position + 1), end
            if first == 0x0E:
                return GclNode("string_resource", int.from_bytes(data[position + 1:end], "little")), end
            if first == 0x06:
                return GclNode("code", int.from_bytes(data[position + 1:end], "little")), end
            return GclNode("value"), end

        if tag in (0xC0, 0xD0, 0xE0, 0xF0):
            return GclNode("integer", (first & 0x3F) - 1), position + 1
        if tag == 0x90 or (first & 0xE0) == 0xA0:
            return GclNode("value"), position + 1
        if tag == 0x40:
            length = 2 if (first & 0x0F) == 0x0F else 1
            end = position + length
            if end > limit:
                raise ToolkitError("Truncated GCL argument.")
            return GclNode("argument"), end
        if tag in (0x10, 0x20):
            end = position + 4
            if end > limit:
                raise ToolkitError("Truncated GCL variable.")
            children: list[GclNode] = []
            if tag == 0x20:
                for _ in range(2):
                    child, end = self.token(data, end, limit)
                    children.append(child)
            return GclNode("variable", children=tuple(children)), end

        actual_tag, size, header_length = self._size(data, position, limit)
        payload_start = position + header_length
        payload_end = payload_start + size
        if payload_end > limit:
            raise ToolkitError("A GCL block points outside its parent block.")
        if actual_tag in (0x30, 0x80):
            return GclNode("block", children=self.stream(data, payload_start, payload_end)), payload_end
        if actual_tag == 0x70:
            if payload_start + 2 > payload_end:
                raise ToolkitError("Truncated GCL procedure call.")
            procedure = int.from_bytes(data[payload_start:payload_start + 2], "little")
            children = self.stream(data, payload_start + 2, payload_end)
            return GclNode("call", procedure, children), payload_end
        if actual_tag == 0x50:
            if payload_start >= payload_end:
                raise ToolkitError("Truncated GCL parameter block.")
            name = chr(data[payload_start])
            children = self.stream(data, payload_start + 1, payload_end)
            return GclNode("parameter", name, children), payload_end
        if actual_tag == 0x60:
            if payload_start + 4 > payload_end:
                raise ToolkitError("Truncated GCL command block.")
            command = int.from_bytes(data[payload_start:payload_start + 3], "little")
            header_size, short_length = self._short_size(
                data, payload_start + 3, payload_end
            )
            header_start = payload_start + 3 + short_length
            header_end = header_start + header_size
            if header_end > payload_end:
                raise ToolkitError("A GCL command header points outside its block.")
            children = (
                self.stream(data, header_start, header_end)
                + self.stream(data, header_end, payload_end)
            )
            return GclNode("command", command, children), payload_end
        raise ToolkitError(f"Unsupported GCL block tag 0x{actual_tag:02X}.")


def _walk_gcl(node: GclNode):
    yield node
    for child in node.children:
        yield from _walk_gcl(child)


def _procedure_records(prefix: bytes, script_data: bytes) -> list[tuple[int, int, bytes]]:
    records: list[tuple[int, int, bytes]] = []
    codec = _GclCodec()
    for index, value in enumerate(_procedure_table(prefix)):
        start = 4 + (value & 0x00FFFFFF)
        _rebuilt, end = codec.token(script_data, start, len(script_data))
        records.append((index, start, bytes(script_data[start:end])))
    return records


def portable_ops_non_english_resources(
    prefix: bytes, script_data: bytes, resources: list
) -> set[int] | None:
    resource_count = len(resources)
    procedures = _procedure_records(prefix, script_data)
    selector = next(
        (index for index, _start, data in procedures if data == PORTABLE_OPS_LANGUAGE_SELECTOR),
        None,
    )

    inspector = _GclInspector()
    roots: list[GclNode] = []
    for _index, start, data in procedures:
        node, end = inspector.token(script_data, start, start + len(data))
        if end != start + len(data):
            raise ToolkitError("A GCX procedure was not inspected completely.")
        roots.append(node)
    main_offset = struct.unpack_from("<I", script_data, 0)[0]
    main_start = 4 + main_offset + 4
    main, _main_end = inspector.token(script_data, main_start, len(script_data))
    roots.append(main)

    groups: set[tuple[int, int, int, int, int]] = set()
    wide_localization_tables: list[tuple[int, ...]] = []
    for root in roots:
        for node in _walk_gcl(root):
            if selector is not None and node.kind == "call" and node.value == selector + 1:
                values = tuple(
                    int(child.value)
                    for child in node.children
                    if child.kind == "integer"
                )
                if len(values) == 5 and all(
                    0 <= values[index] < values[index + 1] < resource_count
                    for index in range(4)
                ):
                    groups.add(values)
            if node.kind != "command" or not node.children or node.children[0].kind != "code":
                continue
            command = node.children[0].value
            parameters = [child for child in node.children if child.kind == "parameter"]
            if command == PORTABLE_OPS_LOCALIZATION_COMMAND:
                for child in parameters:
                    if child.value != "s":
                        continue
                    values = tuple(
                        int(item.value)
                        for item in child.children
                        if item.kind == "integer"
                    )
                    if len(values) == 5 and all(
                        0 <= values[index] < values[index + 1] < resource_count
                        for index in range(4)
                    ):
                        groups.add(values)
                    elif len(values) > 5:
                        wide_localization_tables.append(values)
                continue

            if command == PORTABLE_OPS_FONT_LOCALIZATION_COMMAND:
                for child in parameters:
                    if child.value != "s":
                        continue
                    values = tuple(
                        int(item.value)
                        for item in child.children
                        if item.kind == "integer"
                    )
                    if len(values) == 1 and values[0] + 4 < resource_count:
                        groups.add(tuple(values[0] + index for index in range(5)))
                    elif len(values) == 6:
                        groups.add(values[1:])
                    elif len(values) == 10:
                        groups.add(values[:5])
                        groups.add(values[5:])
                continue

            if command == PORTABLE_OPS_NAMING_LOCALIZATION_COMMAND:
                bases: dict[str, int] = {}
                for child in parameters:
                    integers = [
                        int(item.value)
                        for item in child.children
                        if item.kind == "integer"
                    ]
                    if child.value in "egfsi" and len(integers) == 1:
                        bases[str(child.value)] = integers[0]
                if all(name in bases for name in "egfsi"):
                    values = tuple(bases[name] for name in "egfsi")
                    if all(
                        0 <= values[index] < values[index + 1] < resource_count
                        for index in range(4)
                    ):
                        groups.add(values)

    if wide_localization_tables:
        if (
            len(wide_localization_tables) != 5
            or len({len(values) for values in wide_localization_tables}) != 1
            or any(not values for values in wide_localization_tables)
        ):
            raise ToolkitError(
                "A European Portable Ops wide localization table has an unknown layout."
            )
        values = tuple(table[0] for table in wide_localization_tables)
        if not all(
            0 <= values[index] < values[index + 1] < resource_count
            for index in range(4)
        ):
            raise ToolkitError(
                "A European Portable Ops wide localization table has invalid boundaries."
            )
        groups.add(values)
    if not groups:
        return None

    all_bases = sorted({value for group in groups for value in group})

    def terminator_key(index: int) -> tuple[int, bytes]:
        resource = resources[index]
        payload = resource.data
        if resource.is_string:
            payload = payload.split(b"\0", 1)[0]
        return resource.kind_flag, payload

    non_english: set[int] = set()
    for group in groups:
        for start, end in zip(group[1:], group[2:]):
            non_english.update(range(start, end))

        last_start = group[-1]
        next_base = next(
            (value for value in all_bases if value > last_start),
            resource_count,
        )
        lengths = [group[index + 1] - group[index] for index in range(4)]
        if len(set(lengths)) == 1:
            last_end = last_start + lengths[0]
        else:
            terminators = [terminator_key(group[index + 1] - 1) for index in range(4)]
            if len(set(terminators)) != 1:
                raise ToolkitError(
                    "A European Portable Ops localization group has inconsistent terminators."
                )
            last_end = next(
                (
                    index + 1
                    for index in range(last_start, next_base)
                    if terminator_key(index) == terminators[0]
                ),
                -1,
            )
            if last_end < 0:
                raise ToolkitError(
                    "The final European Portable Ops language block has no matching terminator."
                )
        if not (last_start < last_end <= next_base):
            raise ToolkitError(
                "A European Portable Ops localization resource group has invalid boundaries."
            )
        non_english.update(range(last_start, last_end))
    return non_english


class _GclCodec:
    def __init__(self, replacements: dict[int, bytes] | None = None) -> None:
        self.replacements = replacements or {}
        self.strings: list[GclString] = []

    @staticmethod
    def _size_header(tag: int, size: int) -> bytes:
        if size <= 0x0C:
            return bytes((tag | size,))
        if size <= 0xFF:
            return bytes((tag | 0x0D, size))
        if size <= 0xFFFF:
            return bytes((tag | 0x0E, size & 0xFF, size >> 8))
        raise ToolkitError("A rebuilt GCL block exceeds its 16-bit size limit.")

    @staticmethod
    def _read_size(data: bytes, position: int) -> tuple[int, int, int]:
        first = data[position]
        code = first & 0x0F
        if code == 0x0D:
            if position + 2 > len(data):
                raise ToolkitError("Truncated GCL block size.")
            return first & 0xF0, data[position + 1], 2
        if code == 0x0E:
            if position + 3 > len(data):
                raise ToolkitError("Truncated GCL block size.")
            return first & 0xF0, int.from_bytes(data[position + 1:position + 3], "little"), 3
        return first & 0xF0, code, 1

    @staticmethod
    def _short_size(data: bytes, position: int) -> tuple[int, int]:
        if position >= len(data):
            raise ToolkitError("Truncated GCL command header.")
        first = data[position]
        if first <= 0x7F:
            return first, 1
        if position + 2 > len(data):
            raise ToolkitError("Truncated GCL command header.")
        return ((first << 8) | data[position + 1]) & 0x7FFF, 2

    @staticmethod
    def _short_header(size: int) -> bytes:
        if size <= 0x7F:
            return bytes((size,))
        if size <= 0x7FFF:
            return bytes((0x80 | (size >> 8), size & 0xFF))
        raise ToolkitError("A rebuilt GCL command header exceeds its 15-bit size limit.")

    def stream(self, data: bytes, start: int = 0, end: int | None = None) -> tuple[bytes, int]:
        limit = len(data) if end is None else end
        position = start
        output = bytearray()
        while position < limit:
            token, position = self.token(data, position, limit)
            output += token
        if position != limit:
            raise ToolkitError("A GCL token crossed its parent block boundary.")
        return bytes(output), position

    def token(self, data: bytes, position: int, limit: int) -> tuple[bytes, int]:
        if position >= limit:
            raise ToolkitError("Unexpected end of GCL data.")
        first = data[position]
        tag = first & 0xF0

        if tag == 0:
            fixed = {0x00: 0, 0x01: 2, 0x02: 1, 0x03: 1, 0x04: 1,
                     0x06: 3, 0x08: 2, 0x09: 4, 0x0A: 4, 0x0D: 4, 0x0E: 2}
            if first == 0x07:
                if position + 2 > limit:
                    raise ToolkitError("Truncated inline GCL string.")
                length = data[position + 1]
                end = position + 2 + length
                if end > limit or length == 0 or data[end - 1] != 0:
                    raise ToolkitError("Invalid inline GCL string record.")
                raw = data[position + 2:end - 1]
                index = len(self.strings)
                self.strings.append(GclString(index, raw))
                replacement = self.replacements.get(index, raw)
                if b"\0" in replacement:
                    raise ToolkitError("Inline GCL text cannot contain a NUL byte.")
                rebuilt_length = len(replacement) + 1
                if rebuilt_length > 0xFF:
                    raise ToolkitError("An inline GCL string exceeds its one-byte record limit (254 bytes).")
                return b"\x07" + bytes((rebuilt_length,)) + replacement + b"\0", end
            if first not in fixed:
                raise ToolkitError(f"Unsupported GCL value type 0x{first:02X}.")
            end = position + 1 + fixed[first]
            if end > limit:
                raise ToolkitError("Truncated GCL value.")
            return data[position:end], end

        if tag in (0xC0, 0xD0, 0xE0, 0xF0, 0x90) or (first & 0xE0) == 0xA0:
            return data[position:position + 1], position + 1

        if tag == 0x40:
            length = 2 if (first & 0x0F) == 0x0F else 1
            end = position + length
            if end > limit:
                raise ToolkitError("Truncated GCL argument.")
            return data[position:end], end

        if tag in (0x10, 0x20):
            end = position + 4
            if end > limit:
                raise ToolkitError("Truncated GCL variable.")
            output = bytearray(data[position:end])
            if tag == 0x20:
                for _ in range(2):
                    nested, end = self.token(data, end, limit)
                    output += nested
            return bytes(output), end

        if tag not in (0x30, 0x50, 0x60, 0x70, 0x80):
            raise ToolkitError(f"Unsupported GCL tag 0x{tag:02X}.")
        actual_tag, size, header_length = self._read_size(data, position)
        payload_start = position + header_length
        payload_end = payload_start + size
        if payload_end > limit:
            raise ToolkitError("A GCL block points outside its parent block.")
        payload = data[payload_start:payload_end]

        if actual_tag in (0x30, 0x80):
            rebuilt, _ = self.stream(payload)
        elif actual_tag == 0x70:
            if len(payload) < 2:
                raise ToolkitError("Truncated GCL evaluation block.")
            tail, _ = self.stream(payload, 2)
            rebuilt = payload[:2] + tail
        elif actual_tag == 0x50:
            if not payload:
                raise ToolkitError("Truncated GCL parameter block.")
            tail, _ = self.stream(payload, 1)
            rebuilt = payload[:1] + tail
        else:
            if len(payload) < 4:
                raise ToolkitError("Truncated GCL command block.")
            command = payload[:3]
            header_size, short_length = self._short_size(payload, 3)
            header_start = 3 + short_length
            header_end = header_start + header_size
            if header_end > len(payload):
                raise ToolkitError("A GCL command header points outside its block.")
            header, _ = self.stream(payload, header_start, header_end)
            tail, _ = self.stream(payload, header_end)
            rebuilt = command + self._short_header(len(header)) + header + tail

        return self._size_header(actual_tag, len(rebuilt)) + rebuilt, payload_end


def parse_gcl_resource(data: bytes) -> list[GclString]:
    codec = _GclCodec()
    position = 0
    rebuilt = bytearray()
    while position < len(data):
        is_end = data[position] == 0
        token, position = codec.token(data, position, len(data))
        rebuilt += token
        if is_end:
            break
    if bytes(rebuilt) + data[position:] != data:
        raise ToolkitError("Unchanged GCL resource did not rebuild byte-identically.")
    return codec.strings


def build_gcl_resource(data: bytes, replacements: dict[int, bytes]) -> bytes:
    codec = _GclCodec(replacements)
    position = 0
    rebuilt = bytearray()
    while position < len(data):
        is_end = data[position] == 0
        token, position = codec.token(data, position, len(data))
        rebuilt += token
        if is_end:
            break
    return bytes(rebuilt) + data[position:]


def _procedure_table(prefix: bytes) -> list[int]:
    if len(prefix) < 8 or (len(prefix) - 8) % 4:
        raise ToolkitError("The GCX procedure table has an invalid size.")
    values = [struct.unpack_from("<i", prefix, offset)[0] for offset in range(4, len(prefix) - 4, 4)]
    if struct.unpack_from("<i", prefix, len(prefix) - 4)[0] != -1:
        raise ToolkitError("The GCX procedure table is not terminated.")
    return values


def parse_gcl_script(prefix: bytes, script_data: bytes) -> list[GclScriptString]:
    if len(script_data) < 8:
        raise ToolkitError("The GCX procedure data is truncated.")
    values = _procedure_table(prefix)
    main_offset = struct.unpack_from("<I", script_data, 0)[0]
    records: list[GclScriptString] = []
    for procedure_index, value in enumerate(values):
        start = 4 + (value & 0x00FFFFFF)
        codec = _GclCodec()
        rebuilt, end = codec.token(script_data, start, len(script_data))
        if rebuilt != script_data[start:end]:
            raise ToolkitError("A GCX procedure did not rebuild byte-identically.")
        records.extend(
            GclScriptString("procedure", procedure_index, item.index, item.data)
            for item in codec.strings
        )
    main_size_position = 4 + main_offset
    if main_size_position + 4 > len(script_data):
        raise ToolkitError("The GCX main procedure points outside the file.")
    main_start = main_size_position + 4
    codec = _GclCodec()
    rebuilt, end = codec.token(script_data, main_start, len(script_data))
    if rebuilt != script_data[main_start:end]:
        raise ToolkitError("The GCX main procedure did not rebuild byte-identically.")
    records.extend(GclScriptString("main", 0, item.index, item.data) for item in codec.strings)
    return records


def build_gcl_script(
    prefix: bytes,
    script_data: bytes,
    replacements: dict[tuple[str, int, int], bytes],
) -> tuple[bytes, bytes]:
    values = _procedure_table(prefix)
    main_offset = struct.unpack_from("<I", script_data, 0)[0]
    intervals: dict[int, tuple[int, bytes, str, int]] = {}
    relocated_starts: dict[int, int] = {}

    for procedure_index, value in enumerate(values):
        start = 4 + (value & 0x00FFFFFF)
        codec = _GclCodec({
            string_index: payload
            for (kind, index, string_index), payload in replacements.items()
            if kind == "procedure" and index == procedure_index
        })
        rebuilt, end = codec.token(script_data, start, len(script_data))
        existing = intervals.get(start)
        if existing is not None and (existing[0] != end or existing[1] != rebuilt):
            raise ToolkitError("Overlapping GCX procedure records cannot be rebuilt safely.")
        intervals[start] = (end, rebuilt, "procedure", procedure_index)

    main_size_position = 4 + main_offset
    main_start = main_size_position + 4
    main_codec = _GclCodec({
        string_index: payload
        for (kind, _index, string_index), payload in replacements.items()
        if kind == "main"
    })
    rebuilt_main, main_end = main_codec.token(script_data, main_start, len(script_data))
    intervals[main_size_position] = (
        main_end,
        struct.pack("<I", len(rebuilt_main)) + rebuilt_main,
        "main",
        0,
    )

    output = bytearray()
    output += script_data[:4]
    cursor = 4
    new_main_offset: int | None = None
    for start in sorted(intervals):
        end, rebuilt, kind, index = intervals[start]
        if start < cursor:
            if end <= cursor:
                continue
            raise ToolkitError("GCX procedure records overlap.")
        output += script_data[cursor:start]
        new_start = len(output)
        if kind == "main":
            new_main_offset = new_start - 4
        else:
            relocated_starts[start] = new_start - 4
        output += rebuilt
        cursor = end
    output += script_data[cursor:]
    if new_main_offset is None:
        raise ToolkitError("The GCX main procedure was not rebuilt.")
    struct.pack_into("<I", output, 0, new_main_offset)

    rebuilt_prefix = bytearray(prefix)
    for index, value in enumerate(values):
        original_start = 4 + (value & 0x00FFFFFF)
        new_offset = relocated_starts.get(original_start)
        if new_offset is None:
            raise ToolkitError("A GCX procedure table entry could not be relocated.")
        if new_offset > 0x00FFFFFF:
            raise ToolkitError("GCX procedure data exceeds its 24-bit offset limit.")
        struct.pack_into("<I", rebuilt_prefix, 4 + index * 4, (value & 0xFF000000) | new_offset)
    return bytes(rebuilt_prefix), bytes(output)


PORTABLE_OPS_CODEC_ACTOR = 0x4A243A
PORTABLE_OPS_CODEC_COMMAND = 0x82BC9
PORTABLE_OPS_SLOT_COMMAND = 0x6592A7


def _codec_slot_target(old_value: int, old_start_to_index: dict[int, int], old_need: dict[int, int] | None = None) -> int | None:
    """Codec slot: upper 12 bits = 2048-byte read count; lower 20 = offset/16.

    MGS2 CDC_CalcFileParamByEntry and PO's SRL 20 / INS 20,12 loader
    split the same fields. A high byte of 01 is a common read size, not a file ID.
    """
    unsigned = old_value & 0xFFFFFFFF
    capacity = unsigned >> 20
    if not capacity:
        return None
    block = old_start_to_index.get((unsigned & 0xFFFFF) * 16)
    if block is None or capacity < (old_need or {}).get(block, 0):
        return None
    return block


def _codec_target_nodes(root: GclNode, starts: dict[int, int], need: dict[int, int] | None = None) -> list[GclNode]:
    """Only procedure arguments and NewCodec parameters can carry codec slots."""
    targets = []
    for node in _walk_gcl(root):
        candidates = ()
        if node.kind == 'call':
            candidates = node.children
        elif node.kind == 'command' and node.value == PORTABLE_OPS_CODEC_COMMAND:
            if not any(c.kind == 'code' and c.value == PORTABLE_OPS_CODEC_ACTOR for c in node.children):
                continue
            candidates = tuple(item for c in node.children if c.kind == 'parameter' for item in c.children)
        for item in candidates:
            if item.kind == 'integer' and isinstance(item.value, int) and _codec_slot_target(item.value, starts, need) is not None:
                targets.append(item)
    return targets


def _collect_codec_targets(
    root: GclNode,
    old_start_to_index: dict[int, int],
    old_need: dict[int, int] | None = None,
) -> list[int]:
    return [int(node.value) & 0xFFFFFFFF for node in _codec_target_nodes(root, old_start_to_index, old_need)]


def patch_gcx_codec_refs(
    prefix: bytes,
    script_data: bytes,
    old_start_to_index: dict[int, int],
    old_need: dict[int, int],
    new_starts: list[int],
    new_sizes: list[int],
) -> tuple[bytes, int]:
    if len(new_starts) != len(new_sizes):
        raise ToolkitError("Codec layout tables are inconsistent.")
    new_need = {index: (size + 2047) // 2048 for index, size in enumerate(new_sizes)}
    inspector = _GclInspector()
    intervals: dict[int, tuple[int, bytes]] = {}
    for _index, start, raw in _procedure_records(prefix, script_data):
        if start not in intervals:
            intervals[start] = (start + len(raw), raw)
    try:
        main_offset = struct.unpack_from("<I", script_data, 0)[0]
        main_start = 4 + main_offset + 4
        _main_node, main_end = inspector.token(script_data, main_start, len(script_data))
        if main_start not in intervals:
            intervals[main_start] = (main_end, bytes(script_data[main_start:main_end]))
    except ToolkitError:
        pass
    patched = bytearray(script_data)
    total = 0
    for start in sorted(intervals):
        end, _raw = intervals[start]
        original = bytes(patched[start:end])
        try:
            root, parsed_end = inspector.token(original, 0, len(original))
        except ToolkitError:
            continue
        if parsed_end != len(original):
            continue
        # Rewrite only parsed 32-bit integer payloads. Byte-pattern replacement
        # also matched the same bytes inside inline strings and nested payloads.
        rebuilt = bytearray(original)
        for node in _codec_target_nodes(root, old_start_to_index, old_need):
            if node.integer_offset is None:
                raise ToolkitError('Codec reference has no 32-bit integer payload.')
            old_value = int(node.value) & 0xFFFFFFFF
            block_index = _codec_slot_target(old_value, old_start_to_index, old_need)
            if block_index is None:
                continue
            new_start = new_starts[block_index]
            capacity = max(old_value >> 20, new_need[block_index])
            if new_start % 16 or new_start // 16 > 0xFFFFF or capacity > 0xFFF:
                raise ToolkitError('Rebuilt codec reference exceeds its 20-bit offset or 12-bit size.')
            new_value = (capacity << 20) | (new_start // 16)
            if new_value != old_value:
                struct.pack_into('<I', rebuilt, node.integer_offset, new_value)
                total += 1
        patched[start:end] = rebuilt
    if total:
        _verify_patched_script(prefix, bytes(patched))
    return bytes(patched), total


def codec_reference_indices(prefix: bytes, script_data: bytes, starts: dict[int, int]) -> set[int]:
    return set(codec_reference_capacities(prefix, script_data, starts))


def codec_reference_capacities(prefix: bytes, script_data: bytes, starts: dict[int, int], need: dict[int, int] | None = None) -> dict[int, int]:
    inspector = _GclInspector()
    roots = []
    for _index, start, raw in _procedure_records(prefix, script_data):
        root, end = inspector.token(raw, 0, len(raw))
        if end != len(raw):
            raise ToolkitError('A codec reference procedure was not consumed completely.')
        roots.append(root)
    main_start = struct.unpack_from('<I', script_data)[0] + 8
    roots.append(inspector.token(script_data, main_start, len(script_data))[0])
    capacities: dict[int, int] = {}
    for root in roots:
        for value in _collect_codec_targets(root, starts, need):
            index = starts[(value & 0xFFFFF)*16]
            capacities[index] = max(capacities.get(index, 0), value >> 20)
    return capacities


def _verify_patched_script(prefix: bytes, script_data: bytes) -> None:
    values = _procedure_table(prefix)
    codec = _GclCodec()
    for value in values:
        start = 4 + (value & 0x00FFFFFF)
        codec.token(script_data, start, len(script_data))
    main_offset = struct.unpack_from("<I", script_data, 0)[0]
    main_start = 4 + main_offset + 4
    codec.token(script_data, main_start, len(script_data))


def _collect_slot_targets(
    root: GclNode,
    mapping: dict[int, int],
    old_start_to_index: dict[int, int],
    old_need: dict[int, int],
) -> tuple[dict[int, int], dict[str, int]]:
    from collections import Counter as _Counter

    in_scope: dict[int, int] = _Counter()
    out_of_scope: dict[str, int] = _Counter()
    stack: list[tuple[GclNode, tuple[GclNode, ...]]] = [(root, ())]
    while stack:
        node, ancestors = stack.pop()
        if node.kind == "integer" and isinstance(node.value, int):
            unsigned = node.value & 0xFFFFFFFF
            if unsigned in mapping:
                parent = ancestors[-1] if ancestors else None
                grandparent = ancestors[-2] if len(ancestors) >= 2 else None
                codec_claimed = False
                slot_scoped = False
                context = "root"
                if parent is not None and parent.kind == "call" and node in parent.children:
                    context = f"call-{parent.value}"
                    if _codec_slot_target(unsigned, old_start_to_index, old_need) is not None:
                        codec_claimed = True
                elif (
                    parent is not None
                    and parent.kind == "command"
                    and parent.value in (PORTABLE_OPS_CODEC_COMMAND, PORTABLE_OPS_SLOT_COMMAND)
                    and node in parent.children
                ):
                    context = f"command-{parent.value:x}-direct"
                    if _codec_slot_target(unsigned, old_start_to_index, old_need) is not None:
                        pass
                    else:
                        slot_scoped = True
                elif (
                    parent is not None
                    and parent.kind == "parameter"
                    and grandparent is not None
                    and grandparent.kind == "command"
                    and grandparent.value
                    in (PORTABLE_OPS_CODEC_COMMAND, PORTABLE_OPS_SLOT_COMMAND)
                ):
                    codes = [child.value for child in grandparent.children if child.kind == "code"]
                    context = f"command-{grandparent.value:x}-param-{parent.value}"
                    if (
                        grandparent.value == PORTABLE_OPS_CODEC_COMMAND
                        and PORTABLE_OPS_CODEC_ACTOR in codes
                        and _codec_slot_target(unsigned, old_start_to_index, old_need) is not None
                    ):
                        codec_claimed = True
                    else:
                        slot_scoped = True
                if codec_claimed:
                    pass
                elif slot_scoped:
                    in_scope[unsigned] += 1
                else:
                    out_of_scope[f"{context} value {unsigned:#x}"] += 1
        for child in node.children:
            stack.append((child, ancestors + (node,)))
    return in_scope, out_of_scope


def patch_gcx_slot_refs(
    prefix: bytes,
    script_data: bytes,
    mapping: dict[int, int],
    old_start_to_index: dict[int, int],
    old_need: dict[int, int],
) -> tuple[bytes, int]:
    moves = {old: new for old, new in mapping.items() if new != old}
    if not moves:
        return script_data, 0
    inspector = _GclInspector()
    intervals: dict[int, tuple[int, bytes]] = {}
    for _index, start, raw in _procedure_records(prefix, script_data):
        if start not in intervals:
            intervals[start] = (start + len(raw), raw)
    try:
        main_offset = struct.unpack_from("<I", script_data, 0)[0]
        main_start = 4 + main_offset + 4
        _main_node, main_end = inspector.token(script_data, main_start, len(script_data))
        if main_start not in intervals:
            intervals[main_start] = (main_end, bytes(script_data[main_start:main_end]))
    except ToolkitError:
        pass
    patched = bytearray(script_data)
    total = 0
    for start in sorted(intervals):
        end, _raw = intervals[start]
        original = bytes(patched[start:end])
        try:
            root, parsed_end = inspector.token(original, 0, len(original))
        except ToolkitError:
            continue
        if parsed_end != len(original):
            continue
        in_scope, out_of_scope = _collect_slot_targets(root, moves, old_start_to_index, old_need)
        if out_of_scope:
            detail = "; ".join(f"{context} x{count}" for context, count in sorted(out_of_scope.items()))
            raise ToolkitError(f"Slot reference outside the known command scope: {detail}.")
        if not in_scope:
            continue
        replacements: dict[bytes, bytes] = {}
        for old_value, expected in sorted(in_scope.items()):
            old_byte = struct.pack("<I", old_value)
            if original.count(bytes((0x09,)) + old_byte):
                raise ToolkitError("Unexpected signed slot reference encoding.")
            pattern = bytes((0x0A,)) + old_byte
            actual = original.count(pattern)
            if actual != expected:
                raise ToolkitError(
                    f"Slot reference byte count does not match its script structure for {old_value:#x}."
                )
            replacements[pattern] = bytes((0x0A,)) + struct.pack("<I", moves[old_value])
        rebuilt = bytearray()
        position = 0
        while position < len(original):
            if original[position] in (0x09, 0x0A) and position + 5 <= len(original):
                window = original[position:position + 5]
                replacement = replacements.get(window)
                if replacement is not None:
                    rebuilt += replacement
                    position += 5
                    total += 1
                    continue
            rebuilt.append(original[position])
            position += 1
        if len(rebuilt) != len(original):
            raise ToolkitError("Slot relocation changed its script size.")
        patched[start:end] = rebuilt
    if total:
        _verify_patched_script(prefix, bytes(patched))
    return bytes(patched), total
