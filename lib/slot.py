from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from archives import NamedEntry, build_zar, parse_zar
from codec import codec_file_starts
from common import ToolkitError, align, sha256_bytes
from gcl import _codec_slot_target, patch_gcx_slot_refs
from gcx import build_gcx, parse_gcx


SECTOR_SIZE = 0x800
SECTION_END = 0x7F000000
SECTION_CACHE = 0x7F000002


@dataclass
class SlotAsset:
    index: int
    tag_index: int
    asset_id: int
    offset: int
    data: bytes
    absolute_offset: int

    @property
    def extension_code(self) -> int:
        return self.asset_id >> 24


@dataclass
class SlotPage:
    index: int
    start: int
    end: int
    data_start: int
    header: bytes
    tail_padding: bytes
    tags: list[tuple[int, int]]
    assets: list[SlotAsset]

    @property
    def sector_start(self) -> int:
        return self.start // SECTOR_SIZE


@dataclass
class SlotFile:
    first_sector: bytes
    pages: list[SlotPage]


def slot_page_code(start_sector: int, sector_count: int) -> int:
    if start_sector > 0xFFFFF or sector_count > 0xFFF:
        raise ToolkitError("SLOT.DAT page exceeds the 20-bit start or 12-bit size limit.")
    return (sector_count << 20) | start_sector


def parse_slot(data: bytes) -> SlotFile:
    if len(data) < SECTOR_SIZE or len(data) % SECTOR_SIZE:
        raise ToolkitError("slot.dat is not aligned to 0x800-byte sectors.")
    pages: list[SlotPage] = []
    page_start = SECTOR_SIZE
    while page_start < len(data):
        if page_start + 4 > len(data):
            raise ToolkitError("slot.dat page header is truncated.")
        tag_count = struct.unpack_from("<I", data, page_start)[0]
        if tag_count < 3 or tag_count > 10_000:
            raise ToolkitError(f"Invalid slot.dat tag count at 0x{page_start:X}: {tag_count}")
        table_end = page_start + 4 + tag_count * 8
        if table_end > len(data):
            raise ToolkitError("slot.dat tag table is truncated.")
        tags = [struct.unpack_from("<II", data, page_start + 4 + index * 8) for index in range(tag_count)]
        data_start = align(table_end, SECTOR_SIZE)
        section_sizes = [value for tag_id, value in tags if tag_id == SECTION_END]
        if len(section_sizes) != 1:
            raise ToolkitError(f"slot.dat page at 0x{page_start:X} has an unsupported section layout.")
        data_size = section_sizes[0]
        page_end = data_start + align(data_size, SECTOR_SIZE)
        if page_end > len(data):
            raise ToolkitError("slot.dat page extends past the file.")
        asset_tags = [
            (tag_index, tag_id, offset)
            for tag_index, (tag_id, offset) in enumerate(tags)
            if tag_id != 0 and (tag_id >> 28) != 7
        ]
        assets: list[SlotAsset] = []
        for asset_index, (tag_index, tag_id, offset) in enumerate(asset_tags):
            next_offset = asset_tags[asset_index + 1][2] if asset_index + 1 < len(asset_tags) else data_size
            if offset > next_offset or next_offset > data_size:
                raise ToolkitError(f"slot.dat asset offsets are invalid at page 0x{page_start:X}.")
            absolute = data_start + offset
            assets.append(
                SlotAsset(
                    index=asset_index,
                    tag_index=tag_index,
                    asset_id=tag_id,
                    offset=offset,
                    data=bytes(data[absolute:data_start + next_offset]),
                    absolute_offset=absolute,
                )
            )
        pages.append(
            SlotPage(
                len(pages),
                page_start,
                page_end,
                data_start,
                bytes(data[page_start:data_start]),
                bytes(data[data_start + data_size:page_end]),
                tags,
                assets,
            )
        )
        page_start = page_end
    if page_start != len(data):
        raise ToolkitError("slot.dat page walk did not end at the file boundary.")
    return SlotFile(bytes(data[:SECTOR_SIZE]), pages)


def build_slot(parsed: SlotFile, replacements: dict[tuple[int, int], bytes] | None = None) -> tuple[bytes, dict[int, int]]:
    replacements = replacements or {}
    out = bytearray(parsed.first_sector)
    page_code_mapping: dict[int, int] = {}
    for page in parsed.pages:
        new_start = len(out)
        tags = list(page.tags)
        page_data = bytearray()
        for asset in page.assets:
            tags[asset.tag_index] = (asset.asset_id, len(page_data))
            page_data += replacements.get((page.index, asset.index), asset.data)
            page_data += b"\0" * (align(len(page_data), 16) - len(page_data))
        total_size = len(page_data)
        for tag_index, (tag_id, value) in enumerate(tags):
            if tag_id in (SECTION_CACHE, SECTION_END):
                tags[tag_index] = (tag_id, total_size)
            elif tag_id == 0:
                tags[tag_index] = (0, 0)
        header = bytearray(page.header)
        struct.pack_into("<I", header, 0, len(tags))
        for tag_index, (tag_id, value) in enumerate(tags):
            struct.pack_into("<II", header, 4 + tag_index * 8, tag_id, value)
        out += header
        out += page_data
        padding_size = align(len(out), SECTOR_SIZE) - len(out)
        if total_size == sum(len(asset.data) for asset in page.assets) and padding_size == len(page.tail_padding):
            out += page.tail_padding
        else:
            out += b"\0" * padding_size
        old_code = slot_page_code(page.sector_start, (page.end - page.start) // SECTOR_SIZE)
        new_code = slot_page_code(new_start // SECTOR_SIZE, (len(out) - new_start) // SECTOR_SIZE)
        page_code_mapping[old_code] = new_code
    return bytes(out), page_code_mapping


def _codec_layout_tables(usrdir: Path) -> tuple[dict[int, int], dict[int, int]]:
    codec_path = usrdir / "codec.dat"
    if not codec_path.is_file():
        raise ToolkitError("codec.dat is missing, so slot references cannot be relocated safely.")
    codec_data = codec_path.read_bytes()
    starts = codec_file_starts(codec_data)
    sizes = [(starts[i + 1] if i + 1 < len(starts) else len(codec_data)) - start for i, start in enumerate(starts)]
    return {start: index for index, start in enumerate(starts)}, {
        index: (size + 2047) // 2048 for index, size in enumerate(sizes)
    }


def _patch_resource_blob(
    blob: bytes,
    mapping: dict[int, int],
    old_start_to_index: dict[int, int],
    old_need: dict[int, int],
) -> tuple[bytes, int]:
    out = bytearray(blob)
    changed = 0
    position = 1
    while position + 4 <= len(out):
        if out[position - 1] != 0x0A:
            position += 1
            continue
        value = struct.unpack_from("<I", out, position)[0]
        replacement = mapping.get(value)
        if replacement is None or replacement == value:
            position += 1
            continue
        if _codec_slot_target(value, old_start_to_index, old_need) is not None:
            raise ToolkitError(
                f"Slot reference {value:#x} in resource data also matches a codec reference."
            )
        struct.pack_into("<I", out, position, replacement)
        changed += 1
        position += 1
    return bytes(out), changed


def patch_gcx_slot_page_codes(
    data: bytes,
    mapping: dict[int, int],
    old_start_to_index: dict[int, int],
    old_need: dict[int, int],
) -> tuple[bytes, int]:
    parsed = parse_gcx(data)
    replacements: dict[int, bytes] = {}
    changed = 0
    for resource in parsed.resources:
        if resource.is_string:
            continue
        patched, count = _patch_resource_blob(resource.data, mapping, old_start_to_index, old_need)
        if count:
            replacements[resource.index] = patched
            changed += count
    try:
        patched_script, script_count = patch_gcx_slot_refs(
            parsed.prefix, parsed.script_data, mapping, old_start_to_index, old_need
        )
    except ToolkitError as exc:
        raise ToolkitError(f"Slot relocation failed in script data: {exc}") from exc
    changed += script_count
    if not changed:
        return data, 0
    return build_gcx(parsed, replacements, script_data=patched_script), changed


def patch_stage_slot_page_codes(usrdir: Path, mapping: dict[int, int]) -> tuple[int, int]:
    old_start_to_index, old_need = _codec_layout_tables(usrdir)
    changed_files = 0
    changed_codes = 0
    for zar_path in sorted((usrdir / "stage").glob("*/_zar"), key=lambda path: path.parent.name.lower()):
        entries = parse_zar(zar_path.read_bytes())
        rebuilt: list[NamedEntry] = []
        file_changes = 0
        for entry in entries:
            if Path(entry.name).suffix.lower() in (".gcx", ".fcx"):
                try:
                    payload, count = patch_gcx_slot_page_codes(
                        entry.data, mapping, old_start_to_index, old_need
                    )
                except ToolkitError as exc:
                    raise ToolkitError(f"Slot relocation failed in {zar_path.parent.name}/{entry.name}: {exc}") from exc
                rebuilt.append(NamedEntry(entry.name, payload, entry.hash_value))
                file_changes += count
            else:
                rebuilt.append(entry)
        if file_changes:
            zar_path.write_bytes(build_zar(rebuilt))
            changed_files += 1
            changed_codes += file_changes
    return changed_files, changed_codes
