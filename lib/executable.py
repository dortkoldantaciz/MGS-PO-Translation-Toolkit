from __future__ import annotations

import struct
from dataclasses import dataclass

from common import (
    MACROMAN_LATIN_CHARACTERS,
    ToolkitError,
    align,
    bytes_to_text,
    text_to_bytes,
)


PT_LOAD = 1
SHT_NOBITS = 8
LANGUAGE_COUNT = 5

# BOOT's debug/system font uses a compact alphabet, not keepfont.row cells.
# Preserve all 51 retail mappings. One two-byte padding slot after that table
# can hold an alias from uppercase Ÿ (1F5A) to the existing ÿ menu glyph.
DECODER_CELL_LOAD = 0x95C4
DECODER_CELL_ADD = 0x95C8
DECODER_MISS_JUMP = 0x95B4
DECODER_TABLE_COUNT = 0x95A8
DECODER_TABLE_ADDRESS = 0x1F7250
MIPS_NOP = 0x00000000
MIPS_ADDU_V0_A2 = 0x00C01021


@dataclass(frozen=True)
class GlyphCell:
    """One BOOT lookup entry: key (code + 0x7F) -> compact system-font cell (raw + 0x20)."""

    index: int
    file_offset: int
    raw: int
    key: int

    @property
    def code(self) -> int:
        return (self.key - 0x7F) & 0xFF

    @property
    def cell(self) -> int:
        return self.raw + 0x20



@dataclass(frozen=True)
class ElfSection:
    index: int
    header_offset: int
    name: str
    kind: int
    address: int
    offset: int
    size: int


@dataclass(frozen=True)
class ElfSegment:
    index: int
    header_offset: int
    kind: int
    offset: int
    address: int
    file_size: int
    memory_size: int


@dataclass(frozen=True)
class BootUiText:
    index: int
    pointer_offset: int
    data: bytes


@dataclass(frozen=True)
class BootUiTable:
    data: bytes
    table_offset: int
    pointer_count: int
    records: tuple[BootUiText, ...]
    sections: tuple[ElfSection, ...]
    segments: tuple[ElfSegment, ...]
    section_table_offset: int
    section_entry_size: int


BOOT_ACCENT_BYTES_TO_TEXT = {
    b"&`A": "À",
    b"&'A": "Á",
    b"&^A": "Â",
    b"&~A": "Ä",
    b"&%A": "Å",
    b"&~Q": "Ç",
    b"&`E": "È",
    b"&'E": "É",
    b"&^E": "Ê",
    b"&~E": "Ë",
    b"&`I": "Ì",
    b"&'I": "Í",
    b"&^I": "Î",
    b"&~I": "Ï",
    b"&`O": "Ò",
    b"&'O": "Ó",
    b"&^O": "Ô",
    b"&~O": "Ö",
    b"&`U": "Ù",
    b"&'U": "Ú",
    b"&^U": "Û",
    b"&~U": "Ü",
    b"&'Y": "Ý",
    b"&~y": "ÿ",
    b"&@E": "Æ",
    b"&~s": "ß",
    b"&~N": "Ñ",
    b"&~?": "¿",
    b"&~!": "¡",
    b"&&": "&",
}
BOOT_TEXT_TO_ACCENT_BYTES = {
    character: encoded for encoded, character in BOOT_ACCENT_BYTES_TO_TEXT.items()
}
MACROMAN_BYTE = {
    character: code for code, character in MACROMAN_LATIN_CHARACTERS.items()
}
BOOT_COMPACT_SUPPORTED_CODES = {
    0x4C, 0x68, 0x66, 0x01, 0x02, 0x03, 0x6A, 0x04, 0x67, 0x69,
    0x6E, 0x6B, 0x6C, 0x6D, 0x72, 0x6F, 0x70, 0x06, 0x75, 0x73,
    0x74, 0x07, 0x28, 0x09, 0x08, 0x0A, 0x0B, 0x0E, 0x10, 0x0F,
    0x11, 0x12, 0x14, 0x13, 0x15, 0x16, 0x19, 0x18, 0x1A, 0x1B,
    0x1E, 0x1D, 0x1F, 0x20, 0x59, 0x4F, 0x50, 0x42, 0x41, 0x17,
    0x05, 0x5A,
}


def boot_bytes_to_text(raw: bytes) -> str:
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    if not raw:
        return "<EMPTY>"
    expanded: list[str] = []
    plain = bytearray()

    def flush_plain() -> None:
        if plain:
            raw_plain = bytes(plain)
            try:
                decoded = raw_plain.decode("utf-8")
            except UnicodeDecodeError:
                decoded = raw_plain.decode("latin-1")
            expanded.append(decoded)
            plain.clear()

    position = 0
    while position < len(raw):
        if raw[position] == 0x1F and position + 1 < len(raw):
            code = raw[position + 1]
            character = MACROMAN_LATIN_CHARACTERS.get(code)
            if character is not None:
                flush_plain()
                expanded.append(character)
                position += 2
                continue
        match = next(
            (
                (encoded, character)
                for encoded, character in BOOT_ACCENT_BYTES_TO_TEXT.items()
                if raw.startswith(encoded, position)
            ),
            None,
        )
        if match is None:
            plain.append(raw[position])
            position += 1
            continue
        flush_plain()
        encoded, character = match
        expanded.append(character)
        position += len(encoded)
    flush_plain()
    return "".join(expanded)


def boot_unrenderable_cells(text: str) -> dict[str, int]:
    """Characters absent from both the compact lookup and the accent tokens."""
    unrenderable: dict[str, int] = {}
    for character in text:
        if BOOT_TEXT_TO_ACCENT_BYTES.get(character) is not None:
            continue
        if character == "&" or ord(character) < 0x80:
            continue
        code = MACROMAN_BYTE.get(character)
        if code is not None and code not in BOOT_COMPACT_SUPPORTED_CODES:
            unrenderable[character] = code
    return unrenderable


def text_to_boot_bytes(text: str, unrenderable: dict[str, int] | None = None) -> bytes:
    """Encode one BOOT.BIN UI string.

    BOOT 0x9514 has accent-token and MacRoman-to-compact lookup tables.
    Both address db_font, not the resident keepfont alphabet. Prefer retail
    tokens where available; the import patch adds the missing uppercase Ÿ
    lookup without replacing any existing mapping.
    """
    if text == "<EMPTY>":
        return b"\0"
    out = bytearray()
    position = 0
    while position < len(text):
        if text.startswith("[CODE:", position):
            end = text.find("]", position + 6)
            digits = text[position + 6:end] if end >= 0 else ""
            if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ToolkitError("An invalid [CODE:NNNN] token was found.")
            value = int(digits, 16)
            out += bytes((value >> 8, value & 0xFF))
            position = end + 1
            continue
        if text.startswith("[BYTE:", position):
            end = text.find("]", position + 6)
            digits = text[position + 6:end] if end >= 0 else ""
            if len(digits) != 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ToolkitError("An invalid [BYTE:NN] token was found.")
            out.append(int(digits, 16))
            position = end + 1
            continue
        character = text[position]
        position += 1
        encoded = BOOT_TEXT_TO_ACCENT_BYTES.get(character)
        if encoded is not None:
            out += encoded
        elif character == "&":
            out += b"&&"
        elif ord(character) < 0x80:
            out.append(ord(character))
        else:
            code = MACROMAN_BYTE.get(character)
            if code is None:
                raise ToolkitError(
                    f"BOOT.BIN text cannot encode {character!r}. Use a character the boot "
                    f"screen supports, or write [CODE:1Fxx] with a cell it can reach."
                )
            if unrenderable is not None and code not in BOOT_COMPACT_SUPPORTED_CODES:
                unrenderable[character] = code
            out += bytes((0x1F, code))
    out.append(0)
    return bytes(out)


def _read_elf_layout(data: bytes) -> tuple[
    tuple[ElfSection, ...], tuple[ElfSegment, ...], int, int
]:
    if len(data) < 52 or data[:16] != b"\x7fELF\x01\x01\x01" + b"\0" * 9:
        raise ToolkitError("BOOT.BIN is not a supported little-endian 32-bit ELF file.")
    program_offset = struct.unpack_from("<I", data, 28)[0]
    section_offset = struct.unpack_from("<I", data, 32)[0]
    program_entry_size, program_count = struct.unpack_from("<HH", data, 42)
    section_entry_size, section_count, names_index = struct.unpack_from("<HHH", data, 46)
    if program_entry_size < 32 or section_entry_size < 40:
        raise ToolkitError("BOOT.BIN has invalid ELF table sizes.")
    if program_offset + program_entry_size * program_count > len(data):
        raise ToolkitError("BOOT.BIN program headers are truncated.")
    if section_offset + section_entry_size * section_count > len(data):
        raise ToolkitError("BOOT.BIN section headers are truncated.")
    if names_index >= section_count:
        raise ToolkitError("BOOT.BIN has an invalid section-name table.")

    raw_sections = [
        struct.unpack_from("<IIIIIIIIII", data, section_offset + index * section_entry_size)
        for index in range(section_count)
    ]
    names_header = raw_sections[names_index]
    names_offset, names_size = names_header[4], names_header[5]
    if names_offset + names_size > len(data):
        raise ToolkitError("BOOT.BIN section names are truncated.")
    names = data[names_offset:names_offset + names_size]
    sections: list[ElfSection] = []
    for index, raw in enumerate(raw_sections):
        name_offset = raw[0]
        if name_offset >= len(names):
            raise ToolkitError("BOOT.BIN contains an invalid section name.")
        name_end = names.find(b"\0", name_offset)
        if name_end < 0:
            raise ToolkitError("BOOT.BIN contains an unterminated section name.")
        name = names[name_offset:name_end].decode("ascii")
        sections.append(
            ElfSection(index, section_offset + index * section_entry_size, name, raw[1], raw[3], raw[4], raw[5])
        )

    segments: list[ElfSegment] = []
    for index in range(program_count):
        header_offset = program_offset + index * program_entry_size
        raw = struct.unpack_from("<IIIIIIII", data, header_offset)
        segments.append(ElfSegment(index, header_offset, raw[0], raw[1], raw[2], raw[4], raw[5]))
    return tuple(sections), tuple(segments), section_offset, section_entry_size


def _string_at_address(data: bytes, segments: tuple[ElfSegment, ...], address: int) -> bytes | None:
    for segment in segments:
        if segment.kind != PT_LOAD or not (
            segment.address <= address < segment.address + segment.file_size
        ):
            continue
        offset = segment.offset + address - segment.address
        limit = segment.offset + segment.file_size
        end = data.find(b"\0", offset, limit)
        if end < 0:
            return None
        raw = bytes(data[offset:end + 1])
        body = raw[:-1]
        # Validate complete tokens: the byte after 1F is data, even when its
        # value would be a control byte by itself (e.g. ä=0B and ï=16).
        position = 0
        while position < len(body):
            value = body[position]
            if value == 0x1F:
                if position + 1 >= len(body):
                    return None
                position += 2
                continue
            if value < 0x20 and value not in (9, 10, 13):
                return None
            position += 1
        return raw
    return None


def parse_boot_ui_table(data: bytes) -> BootUiTable:
    sections, segments, section_table_offset, section_entry_size = _read_elf_layout(data)
    section_by_name = {section.name: section for section in sections}
    try:
        data_section = section_by_name[".data"]
    except KeyError as exc:
        raise ToolkitError("BOOT.BIN has no .data section.") from exc

    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    run_count = 0
    for pointer_offset in range(data_section.offset, data_section.offset + data_section.size, 4):
        address = struct.unpack_from("<I", data, pointer_offset)[0]
        if _string_at_address(data, segments, address) is not None:
            if run_start is None:
                run_start = pointer_offset
                run_count = 0
            run_count += 1
        else:
            if run_start is not None and run_count >= 100 and run_count % LANGUAGE_COUNT == 0:
                runs.append((run_start, run_count))
            run_start = None
            run_count = 0
    if run_start is not None and run_count >= 100 and run_count % LANGUAGE_COUNT == 0:
        runs.append((run_start, run_count))
    if len(runs) != 1:
        raise ToolkitError(
            "BOOT.BIN localized UI pointer table could not be identified unambiguously."
        )
    table_offset, pointer_count = runs[0]
    records: list[BootUiText] = []
    for index in range(pointer_count // LANGUAGE_COUNT):
        pointer_offset = table_offset + index * LANGUAGE_COUNT * 4
        address = struct.unpack_from("<I", data, pointer_offset)[0]
        raw = _string_at_address(data, segments, address)
        if raw is None:
            raise ToolkitError(f"BOOT.BIN UI text {index} has an invalid pointer.")
        records.append(BootUiText(index, pointer_offset, raw))
    return BootUiTable(
        data,
        table_offset,
        pointer_count,
        tuple(records),
        sections,
        segments,
        section_table_offset,
        section_entry_size,
    )


def build_boot_ui_table(table: BootUiTable, replacements: dict[int, bytes] | None = None) -> bytes:
    replacements = replacements or {}
    if not replacements:
        return table.data
    valid_indices = {record.index for record in table.records}
    unknown = set(replacements) - valid_indices
    if unknown:
        raise ToolkitError(f"BOOT.BIN replacement index is invalid: {min(unknown)}")
    for index, raw in replacements.items():
        if not raw.endswith(b"\0") or b"\0" in raw[:-1]:
            raise ToolkitError(f"BOOT.BIN UI text {index} is not a single NUL-terminated string.")

    data_segment = next(
        (
            segment
            for segment in table.segments
            if segment.kind == PT_LOAD
            and any(
                section.name == ".data"
                and segment.address <= section.address
                and section.address + section.size <= segment.address + segment.memory_size
                for section in table.sections
            )
        ),
        None,
    )
    if data_segment is None or data_segment.memory_size < data_segment.file_size:
        raise ToolkitError("BOOT.BIN data segment cannot host an expanded UI text pool.")
    insertion_offset = data_segment.offset + data_segment.file_size
    if insertion_offset > len(table.data):
        raise ToolkitError("BOOT.BIN data segment is truncated.")

    pool = bytearray()
    pool_addresses: dict[bytes, int] = {}
    memory_end = data_segment.address + data_segment.memory_size
    pool_base = align(memory_end, 4)
    for index in sorted(replacements):
        raw = replacements[index]
        if raw not in pool_addresses:
            pool_addresses[raw] = pool_base + len(pool)
            pool += raw
            pool += b"\0" * (align(len(pool), 4) - len(pool))
    memory_padding = pool_base - memory_end
    materialized_bss = data_segment.memory_size - data_segment.file_size
    inserted = b"\0" * (materialized_bss + memory_padding) + bytes(pool)
    shift = len(inserted)
    rebuilt = bytearray(table.data[:insertion_offset] + inserted + table.data[insertion_offset:])

    for record in table.records:
        raw = replacements.get(record.index)
        if raw is not None:
            struct.pack_into("<I", rebuilt, record.pointer_offset, pool_addresses[raw])

    new_file_size = data_segment.memory_size + memory_padding + len(pool)
    new_memory_size = new_file_size
    struct.pack_into("<II", rebuilt, data_segment.header_offset + 16, new_file_size, new_memory_size)
    for segment in table.segments:
        if segment.index == data_segment.index or segment.offset < insertion_offset:
            continue
        struct.pack_into("<I", rebuilt, segment.header_offset + 4, segment.offset + shift)

    new_section_table_offset = table.section_table_offset + shift
    struct.pack_into("<I", rebuilt, 32, new_section_table_offset)
    for section in table.sections:
        if section.kind == SHT_NOBITS or section.offset < insertion_offset:
            continue
        new_header = new_section_table_offset + section.index * table.section_entry_size
        struct.pack_into("<I", rebuilt, new_header + 16, section.offset + shift)

    checked = parse_boot_ui_table(bytes(rebuilt))
    for record in checked.records:
        expected = replacements.get(record.index)
        if expected is not None and record.data != expected:
            raise ToolkitError(f"Rebuilt BOOT.BIN UI text {record.index} failed verification.")
    return bytes(rebuilt)

def decode_glyph_cell(code: int) -> int:
    """Resident keepfont cell; BOOT and layout menu fonts use separate mappings."""
    return (code + 0x7F) & 0xFF


def apply_identity_glyph_mapping(data: bytes) -> bytes:
    """Compatibility API: repair the obsolete identity patch, add a safe Ÿ alias.

    The retail 51-entry table maps MacRoman to a *different*, compact font.
    Replacing it by code+127 corrupts valid letters. The old miss jump also
    encoded target bit 27 (0x0A...) instead of a relocatable J (0x08...).
    Only a complete known old patch is repaired; partial/foreign patches fail.
    """
    _sections, segments, _table_offset, _entry_size = _read_elf_layout(data)

    def offset_of(va: int) -> int:
        for segment in segments:
            if segment.kind == PT_LOAD and segment.address <= va < segment.address + segment.file_size:
                return segment.offset + va - segment.address
        raise ToolkitError(f"BOOT.BIN address 0x{va:X} is not in a loaded segment.")

    addresses = (DECODER_MISS_JUMP, DECODER_CELL_LOAD, DECODER_CELL_ADD)
    retail = (0x08002556, 0x90620000, 0x24420020)
    obsolete = (0x0A002571, MIPS_ADDU_V0_A2, MIPS_NOP)
    values = tuple(struct.unpack_from('<I', data, offset_of(a))[0] for a in addresses)
    if values not in (retail, obsolete):
        raise ToolkitError('BOOT.BIN has an unsupported or partially patched system font decoder.')
    output = bytearray(data)
    for address, word in zip(addresses, retail):
        struct.pack_into('<I', output, offset_of(address), word)
    table = offset_of(DECODER_TABLE_ADDRESS)
    # Check the ends of the supported EU table, the existing ÿ mapping, and
    # both legal states of the count/padding pair before changing anything.
    if data[table:table+6] != bytes.fromhex('61CB62E763E5') or data[table+96:table+102] != bytes.fromhex('7CC07B967B84'):
        raise ToolkitError('BOOT.BIN compact font table does not match the European release.')
    if (0x78, 0xD8) not in tuple(zip(data[table:table+102:2], data[table+1:table+102:2])):
        raise ToolkitError('BOOT.BIN has no expected y-diaeresis glyph mapping.')
    count_offset = offset_of(DECODER_TABLE_COUNT)
    count = struct.unpack_from('<I', data, count_offset)[0]
    tail = data[table+102:table+104]
    if (count, tail) not in ((0x2CA40033, b'\0\0'), (0x2CA40034, b'\x78\xD9')):
        raise ToolkitError('BOOT.BIN compact font table count or padding is unsupported.')
    output[table+102:table+104] = b'\x78\xD9'
    struct.pack_into('<I', output, count_offset, 0x2CA40034)
    # The layout-label converter (0x28938) first decodes 1Fxx, then translates
    # MacRoman via .data+10B4 and the accent table at .data+FBC. Plain ASCII
    # replacements are uppercased *before* this step: d/f/n/x become D/F/N/X.
    # Retail has two entries for D8 (ÿ), but none for D9 (Ÿ). Entry 43's ~Y
    # token does not exist in the accent table; lookup continues to entry 65's
    # ~y token. Reuse that redundant entry for Ÿ, keeping every live mapping.
    # Both pointer fields already have PRX relocations to the same segment.
    data_section = next(section for section in _sections if section.name == '.data')
    menu_table = offset_of(data_section.address + 0x10B4)
    spare = menu_table + 43*8
    fallback = menu_table + 65*8
    old_pointer, old_code = struct.unpack_from('<II', data, spare)
    pointer, code = struct.unpack_from('<II', data, fallback)
    def token_at(address: int) -> bytes:
        off = offset_of(address)
        return data[off:off+3]
    if code != 0xD8 or token_at(pointer) != b'~y\0':
        raise ToolkitError('BOOT.BIN menu accent fallback does not match the European release.')
    if (old_code, token_at(old_pointer)) not in ((0xD8,b'~Y\0'), (0xD9,b'~y\0')):
        raise ToolkitError('BOOT.BIN menu accent alias slot has an unsupported mapping.')
    def segment_of(address: int) -> int:
        return next(s.index for s in segments if s.kind == PT_LOAD and s.address <= address < s.address+s.file_size)
    if segment_of(old_pointer) != segment_of(pointer):
        raise ToolkitError('BOOT.BIN menu accent pointers require different relocations.')
    struct.pack_into('<II', output, spare, pointer, 0xD9)
    return bytes(output)
