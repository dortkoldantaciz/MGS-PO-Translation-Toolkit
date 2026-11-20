from __future__ import annotations

import base64
import csv
import io
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Callable

from archives import NamedEntry, build_dar, build_zar, parse_dar, parse_qar, parse_zar
from cap import build_cap, parse_cap
from codec import (
    build_codec,
    build_codec_block,
    codec_file_starts,
    codec_rebuilt_layout,
    english_text_indices,
    parse_codec,
)
from common import (
    APP_NAME,
    APP_VERSION,
    TEXT_LINE_INDEX_NAME,
    TEXT_IMPORT_REPORT_NAME,
    TEXT_MANIFEST_NAME,
    ToolkitError,
    bytes_to_text,
    default_character_map,
    detect_game_region,
    gcl_bytes_to_text,
    prepare_rebuild_root,
    read_json,
    read_text_lines,
    resolve_usrdir,
    sha256_bytes,
    text_to_bytes,
    text_to_gcl_bytes,
    text_to_utf8_bytes,
    utf8_bytes_to_text,
    write_json,
    write_text_lines,
)
from executable import (
    apply_identity_glyph_mapping,
    boot_bytes_to_text,
    boot_unrenderable_cells,
    build_boot_ui_table,
    parse_boot_ui_table,
    text_to_boot_bytes,
)
from font import parse_resident_font
from gcl import (
    build_gcl_resource,
    build_gcl_script,
    parse_gcl_resource,
    parse_gcl_script,
    patch_gcx_codec_refs,
    portable_ops_non_english_resources,
)
from gcx import build_gcx, parse_gcx


Log = Callable[[str], None]
BOOT_SOURCE_FILE = "PSP_GAME/SYSDIR/BOOT.BIN"
# Validate references structurally in every stage, including menu overlays.
NON_CODEC_STAGES: set[str] = set()


def _text_file_stem(kind: str, source_file: str, archive_entry: str = "", dar_entry: str = "") -> str:
    """Editable .txt name: the game container the text lives in (stage, codec, movie, system)."""
    if kind == "codec_text":
        return "codec"
    if kind == "boot_ui_text":
        return "system"
    if kind == "cap_subtitle":
        return "movie"
    if Path(archive_entry).name.lower() == "minifont.fcx":
        return "menus"
    if source_file.startswith("stage/"):
        return "stage"
    if archive_entry:
        return Path(archive_entry).stem.lower()
    return Path(source_file).stem.lower()
STAGE_DIALOGUE_PIXEL_WIDTH = 450
BRIEFING_DIALOGUE_PIXEL_WIDTH = 450
MOVIE_SUBTITLE_PIXEL_WIDTH = 458
MOVIE_SUBTITLE_MAX_LINES = 2


def _log(log: Log | None, message: str) -> None:
    if log:
        log(message)


def _split_string_resource(raw: bytes) -> tuple[bytes, bytes]:
    nul = raw.find(b"\0")
    if nul < 0:
        return raw, b""
    return raw[:nul + 1], raw[nul + 1:]


def _natural_name_key(value: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    text = value.casefold()
    position = 0
    while position < len(text):
        digit = text[position].isdigit()
        end = position + 1
        while end < len(text) and text[end].isdigit() == digit:
            end += 1
        chunk = text[position:end]
        parts.append((1, int(chunk)) if digit else (0, chunk))
        position = end
    return tuple(parts)


def _split_stage_controls(text: str) -> tuple[str, str, str]:
    position = 0
    while position < len(text):
        if text[position] == "|":
            position += 1
            continue
        if text[position] != "<":
            break
        end = text.find(">", position + 1)
        if end < 0:
            break
        position = end + 1
    suffix_start = len(text)
    while suffix_start > position:
        if text[suffix_start - 1] == "|":
            suffix_start -= 1
            continue
        if text[suffix_start - 1] == ">":
            token_start = text.rfind("<", position, suffix_start)
            if token_start >= position:
                suffix_start = token_start
                continue
        break
    return text[position:suffix_start], text[:position], text[suffix_start:]


def _contains_japanese_script(text: str) -> bool:
    for char in text:
        codepoint = ord(char)
        if 0x3040 <= codepoint <= 0x30FF or 0x3400 <= codepoint <= 0x9FFF:
            return True
    return False


def _is_developer_separator(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and all(char in "/-*#" for char in stripped)


def _glyph_code(character: str) -> int:
    if character.isascii():
        return ord(character)
    try:
        encoded = character.encode("mac_roman")
    except UnicodeEncodeError:
        return ord("?")
    return encoded[0] if len(encoded) == 1 else ord("?")


def _glyph_width(character: str, widths: dict[int, int]) -> int:
    return widths.get(_glyph_code(character), widths.get(ord("?"), 10))


def _display_width(text: str, widths: dict[int, int]) -> int:
    width = 0
    position = 0
    while position < len(text):
        if text[position] == "[":
            end = text.find("]", position + 1)
            if end >= 0:
                token = text[position:end + 1]
                if token == "[APOSTROPHE]":
                    width += _glyph_width("'", widths)
                elif token not in ("[B]", "[/B]"):
                    width += widths.get(ord("?"), 10)
                position = end + 1
                continue
        if text[position] == "\\" and position + 1 < len(text):
            width += _glyph_width(text[position + 1], widths)
            position += 2
            continue
        if text[position] == "<":
            end = text.find(">", position + 1)
            if end >= 0:
                position = end + 1
                continue
        width += _glyph_width(text[position], widths)
        position += 1
    return width


def _wrap_segment(text: str, width: int, widths: dict[int, int]) -> list[str]:
    wrapped: list[str] = []
    remaining = text
    while _display_width(remaining, widths) > width:
        position = 0
        visible = 0
        last_space = -1
        while position < len(remaining):
            if remaining[position] == "[":
                end = remaining.find("]", position + 1)
                if end >= 0:
                    token = remaining[position:end + 1]
                    token_width = 0 if token in ("[B]", "[/B]") else widths.get(ord("?"), 10)
                    if token == "[APOSTROPHE]":
                        token_width = _glyph_width("'", widths)
                    if visible + token_width > width:
                        break
                    position = end + 1
                    visible += token_width
                    continue
            if remaining[position] == "\\" and position + 1 < len(remaining):
                unit_width = _glyph_width(remaining[position + 1], widths)
                if visible + unit_width > width:
                    break
                position += 2
                visible += unit_width
                continue
            if remaining[position] == "<":
                end = remaining.find(">", position + 1)
                if end >= 0:
                    position = end + 1
                    continue
            unit_width = _glyph_width(remaining[position], widths)
            if visible + unit_width > width:
                break
            if remaining[position] == " ":
                last_space = position
            position += 1
            visible += unit_width
        if position >= len(remaining):
            break
        if last_space < 0:
            break
        wrapped.append(remaining[:last_space].rstrip(" "))
        remaining = remaining[last_space + 1:].lstrip(" ")
    wrapped.append(remaining)
    return wrapped


def _split_display_lines(text: str, delimiter: str) -> list[str]:
    if delimiter != "|":
        return text.split(delimiter)
    lines: list[str] = []
    start = 0
    position = 0
    while position < len(text):
        if text[position] == "\\" and position + 1 < len(text):
            position += 2
            continue
        if text[position] == "|":
            lines.append(text[start:position])
            start = position + 1
        position += 1
    lines.append(text[start:])
    return lines


def _wrap_display_lines(text: str, delimiter: str, width: int, widths: dict[int, int]) -> list[str]:
    output: list[str] = []
    for line in _split_display_lines(text, delimiter):
        output.extend(_wrap_segment(line, width, widths))
    return output


def _wrap_display_text(text: str, delimiter: str, width: int, widths: dict[int, int]) -> str:
    return delimiter.join(_wrap_display_lines(text, delimiter, width, widths))


def _is_gameplay_stage_dialogue(entry: dict) -> bool:
    if entry.get("kind") != "gcx_string" or "|" not in str(entry.get("control_suffix", "")):
        return False
    parts = Path(str(entry.get("source_file", ""))).parts
    return len(parts) >= 2 and parts[0].casefold() == "stage" and parts[1].casefold().startswith("st")


def _is_briefing_stage_dialogue(entry: dict) -> bool:
    if entry.get("kind") != "gcx_string" or "|" not in str(entry.get("control_suffix", "")):
        return False
    parts = Path(str(entry.get("source_file", ""))).parts
    return (
        len(parts) >= 2
        and parts[0].casefold() == "stage"
        and parts[1].casefold() == "briefing"
    )


def _apply_text_layout(entry: dict, text: str, widths: dict[int, int]) -> str | list[str]:
    if Path(str(entry.get('archive_entry',''))).name.lower() == 'minifont.fcx' or Path(str(entry.get('text_file',''))).stem == 'menus':
        return text  # Reference menu path explicitly disables dialogue wrapping.
    layout = entry.get("layout")
    if layout == "movie_subtitle" or entry.get("kind") == "cap_subtitle":
        lines = _wrap_display_lines(text, "/n", MOVIE_SUBTITLE_PIXEL_WIDTH, widths)
        return [
            "/n".join(lines[position:position + MOVIE_SUBTITLE_MAX_LINES])
            for position in range(0, len(lines), MOVIE_SUBTITLE_MAX_LINES)
        ]
    if layout == "stage_dialogue" or _is_gameplay_stage_dialogue(entry):
        return _wrap_display_text(text, "|", STAGE_DIALOGUE_PIXEL_WIDTH, widths)
    if layout == "briefing_dialogue" or _is_briefing_stage_dialogue(entry):
        return _wrap_display_text(text, "|", BRIEFING_DIALOGUE_PIXEL_WIDTH, widths)
    return text


def _detect_text_profile(codec_data: bytes, codec_block_count: int) -> str:
    if codec_block_count == 272 or b"cr\x1f\x69me" in codec_data:
        return "usa_latin1"
    if codec_block_count == 294 or b"R\x1f\x04PONSE" in codec_data:
        return "europe_macroman"
    raise ToolkitError(
        "This codec.dat does not match the supported USA or European Portable Ops text encoding."
    )


def _load_font_widths(usrdir: Path) -> dict[int, int]:
    init_zar = usrdir / "stage" / "init" / "_zar"
    if not init_zar.is_file():
        raise ToolkitError("The resident font archive stage/init/_zar is missing.")
    for archive_entry in parse_zar(init_zar.read_bytes()):
        if archive_entry.name.casefold() != "cache.dar":
            continue
        for dar_entry in parse_dar(archive_entry.data):
            if dar_entry.name.casefold() != "keepfont.row":
                continue
            header, glyphs = parse_resident_font(dar_entry.data)
            glyph_height = int(header["glyph_height"])
            if glyph_height <= 0:
                raise ToolkitError("The resident font has an invalid glyph height.")
            return {
                int(glyph["code"]): (
                    int(glyph["width"]) * 18 // glyph_height
                    if int(glyph["width"]) > 0
                    else 0
                )
                for glyph in glyphs
            }
    raise ToolkitError("The resident font keepfont.row could not be found in stage/init/_zar.")


def _clean_utf8_display_text(text: str) -> str:
    segments = text.split("|")
    remove: set[int] = set()
    for index, segment in enumerate(segments):
        if not _contains_japanese_script(segment):
            continue
        remove.add(index)
        if index > 0 and _is_developer_separator(segments[index - 1]):
            remove.add(index - 1)
        if index + 1 < len(segments) and _is_developer_separator(segments[index + 1]):
            remove.add(index + 1)
    return "|".join(segment for index, segment in enumerate(segments) if index not in remove).strip("|")


def _has_english_encoded_body(raw: bytes) -> bool:
    ascii_letters = 0
    double_byte_characters = 0
    position = 0
    while position < len(raw) and raw[position] != 0:
        value = raw[position]
        if 0x41 <= value <= 0x5A or 0x61 <= value <= 0x7A:
            ascii_letters += 1
            position += 1
        elif value >= 0x80 and position + 1 < len(raw):
            double_byte_characters += 1
            position += 2
        else:
            position += 1
    if ascii_letters == 0:
        return False
    return not (double_byte_characters >= 3 and double_byte_characters > ascii_letters)


def _append_entry(
    groups: dict[str, list[str]],
    records: list[dict],
    text_stem: str,
    source_file: str,
    kind: str,
    raw_text: bytes,
    trailing: bytes = b"",
    gcl_encoding: bool = False,
    utf8_encoding: bool = False,
    boot_encoding: bool = False,
    text_profile: str = "usa_latin1",
    structural_english: bool = False,
    **location: int | str,
) -> bool:
    lines = groups[text_stem]
    if (
        text_stem not in ("codec", "system")
        and kind in ("gcx_string", "gcl_resource_string")
        and not structural_english
    ):
        if not _has_english_encoded_body(raw_text):
            return False
    if boot_encoding:
        text = boot_bytes_to_text(raw_text)
    elif utf8_encoding:
        text = utf8_bytes_to_text(raw_text)
        text = _clean_utf8_display_text(text)
        if not any(char.isascii() and char.isalpha() for char in text):
            return False
    elif gcl_encoding:
        text = gcl_bytes_to_text(raw_text, text_profile)
    else:
        text = bytes_to_text(raw_text, text_profile)
    if text == "<EMPTY>":
        return False
    control_prefix = ""
    control_suffix = ""
    if kind not in ("codec_text", "boot_ui_text", "cap_subtitle"):
        text, control_prefix, control_suffix = _split_stage_controls(text)
        if (
            not text
            or (kind == "gcx_string" and text.casefold() == "blank")
            or (structural_english and not any(char.isascii() and char.isalpha() for char in text))
        ):
            return False
    if text_stem == "stage":
        import re
        visible = re.sub(r'\[[^\]]*\]|<[^>]*>', '', text)
        letters = ''.join(c for c in visible if c.isalpha())
        if letters and letters.isupper() and not control_prefix:
            text_stem = "menus"
        else:
            text_stem = "dialogue"
        lines = groups[text_stem]
    record = {
        "id": len(records) + 1,
        "text_file": f"texts/{text_stem}.txt",
        "line_number": len(lines) + 1,
        "source_file": source_file,
        "kind": kind,
        "original_text": text,
        "original_text_sha256": sha256_bytes(raw_text),
        "trailing_base64": base64.b64encode(trailing).decode("ascii"),
    }
    if control_prefix or control_suffix:
        record["control_prefix"] = control_prefix
        record["control_suffix"] = control_suffix
    if kind == "cap_subtitle":
        record["layout"] = "movie_subtitle"
        record["max_line_pixels"] = MOVIE_SUBTITLE_PIXEL_WIDTH
        record["max_lines_per_record"] = MOVIE_SUBTITLE_MAX_LINES
    elif _is_gameplay_stage_dialogue(record):
        record["layout"] = "stage_dialogue"
        record["max_line_pixels"] = STAGE_DIALOGUE_PIXEL_WIDTH
    elif _is_briefing_stage_dialogue(record):
        record["layout"] = "briefing_dialogue"
        record["max_line_pixels"] = BRIEFING_DIALOGUE_PIXEL_WIDTH
    record.update(location)
    lines.append(text)
    records.append(record)
    return True


def _merge_duplicate_lines(groups: dict[str, list[str]], records: list[dict]) -> tuple[int, int]:
    """Give identical texts one editable line per file (targets keep their own controls)."""
    by_stem: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_stem[Path(str(record["text_file"])).stem].append(record)
    shared_line_count = 0
    shared_target_count = 0
    for stem, stem_records in by_stem.items():
        merged_lines: list[str] = []
        line_by_text: dict[str, int] = {}
        counts: dict[int, int] = defaultdict(int)
        for record in stem_records:
            text = str(record["original_text"])
            line_number = line_by_text.get(text)
            if line_number is None:
                merged_lines.append(text)
                line_number = len(merged_lines)
                line_by_text[text] = line_number
            record["line_number"] = line_number
            counts[line_number] += 1
        for record in stem_records:
            target_count = counts[int(record["line_number"])]
            if target_count > 1:
                record["shared_target_count"] = target_count
        for count in counts.values():
            if count > 1:
                shared_line_count += 1
                shared_target_count += count
        groups[stem] = merged_lines
    return shared_line_count, shared_target_count


def export_text(source: Path, output_root: Path, log: Log | None = None) -> tuple[Path, Path, int]:
    usrdir, game_root = resolve_usrdir(source)
    output_root = output_root.resolve()
    texts_dir = output_root / "texts"
    if texts_dir.exists():
        shutil.rmtree(texts_dir)
    manifest_path = output_root / TEXT_MANIFEST_NAME
    groups: dict[str, list[str]] = defaultdict(list)
    records: list[dict] = []
    source_hashes: dict[str, str] = {}

    codec_path = usrdir / "codec.dat"
    codec_data = codec_path.read_bytes()
    codec_blocks = parse_codec(codec_data)
    text_profile = _detect_text_profile(codec_data, len(codec_blocks))
    source_hashes["codec.dat"] = sha256_bytes(codec_data)
    for block in codec_blocks:
        structural_indices = set(english_text_indices(block, text_profile))
        for item in block.texts:
            if item.index not in structural_indices:
                continue
            if text_profile != "europe_macroman" and not _has_english_encoded_body(item.data):
                continue
            _append_entry(
                groups, records, "codec", "codec.dat", "codec_text", item.data, item.trailing,
                text_profile=text_profile,
                block_index=block.index, text_index=item.index,
            )
    _log(log, f"codec.dat: {len(groups.get('codec', [])):,} text records")

    boot_path = game_root / Path(BOOT_SOURCE_FILE)
    if boot_path.is_file() and boot_path.read_bytes().startswith(b"\x7fELF"):
        boot_data = boot_path.read_bytes()
        boot_table = parse_boot_ui_table(boot_data)
        source_hashes[BOOT_SOURCE_FILE] = sha256_bytes(boot_data)
        for item in boot_table.records:
            _append_entry(
                groups,
                records,
                "system",
                BOOT_SOURCE_FILE,
                "boot_ui_text",
                item.data,
                boot_encoding=True,
                boot_text_index=item.index,
            )
        _log(log, f"BOOT.BIN: {len(groups.get('system', [])):,} system UI text records")

    stage_files = sorted((usrdir / "stage").glob("*/_zar"), key=lambda path: path.parent.name.lower())
    for stage_number, zar_path in enumerate(stage_files, 1):
        relative = zar_path.relative_to(usrdir).as_posix()
        zar_data = zar_path.read_bytes()
        source_hashes[relative] = sha256_bytes(zar_data)
        before = len(records)
        for archive_entry in parse_zar(zar_data):
            inner_kind = ""
            if archive_entry.name.lower().endswith(".dar"):
                inner_kind = "dar"
            elif archive_entry.name.lower().endswith(".qar"):
                inner_kind = "qar"
            if inner_kind:
                inner_entries = (
                    parse_dar(archive_entry.data) if inner_kind == "dar"
                    else parse_qar(archive_entry.data)
                )
                indexed_caps = list(enumerate(inner_entries))
                indexed_caps.sort(key=lambda item: _natural_name_key(item[1].name))
                for dar_index, dar_entry in indexed_caps:
                    stem = Path(dar_entry.name).stem.lower()
                    if not dar_entry.name.lower().endswith(".cap") or stem.endswith("_j") or "_j_" in stem:
                        continue
                    cap = parse_cap(dar_entry.data)
                    for subtitle in cap.subtitles:
                        if not subtitle.data:
                            continue
                        if text_profile == "europe_macroman" and subtitle.text_flags != 1:
                            continue
                        _append_entry(
                            groups, records,
                            _text_file_stem("cap_subtitle", relative, archive_entry.name, dar_entry.name),
                            relative, "cap_subtitle", subtitle.data,
                            text_profile=text_profile,
                            archive_entry=archive_entry.name, dar_index=dar_index,
                            dar_entry=dar_entry.name, subtitle_index=subtitle.index,
                            start_time=subtitle.start_time, end_time=subtitle.end_time,
                        )
                continue
            if Path(archive_entry.name).suffix.lower() not in (".gcx", ".fcx"):
                continue
            parsed = parse_gcx(archive_entry.data)
            non_english_resources = (
                portable_ops_non_english_resources(
                    parsed.prefix, parsed.script_data, parsed.resources
                )
                if text_profile == "europe_macroman"
                else set()
            )
            for resource in parsed.resources:
                if non_english_resources is not None and resource.index in non_english_resources:
                    continue
                if resource.is_string:
                    text_data, trailing = _split_string_resource(resource.data)
                    _append_entry(
                        groups, records,
                        _text_file_stem("gcx_string", relative, archive_entry.name),
                        relative, "gcx_string", text_data, trailing,
                        text_profile=text_profile,
                        structural_english=non_english_resources is not None,
                        archive_entry=archive_entry.name, resource_index=resource.index,
                    )
                    continue
                utf8_text = resource.data
                utf8_trailing = b""
                try:
                    decoded_utf8 = (
                        utf8_text[:-1].decode("utf-8")
                        if utf8_text.endswith(b"\0") and b"\0" not in utf8_text[:-1]
                        else ""
                    )
                except UnicodeDecodeError:
                    decoded_utf8 = ""
                if decoded_utf8 and all(ord(char) >= 0x20 or char in "\r\n\t" for char in decoded_utf8):
                    _append_entry(
                        groups, records,
                        _text_file_stem("utf8_resource_string", relative, archive_entry.name),
                        relative, "utf8_resource_string", utf8_text, utf8_trailing,
                        text_profile=text_profile,
                        structural_english=non_english_resources is not None,
                        archive_entry=archive_entry.name, resource_index=resource.index, utf8_encoding=True,
                    )
                    continue
                try:
                    inline_strings = parse_gcl_resource(resource.data)
                except ToolkitError:
                    continue
                for item in inline_strings:
                    _append_entry(
                        groups, records,
                        _text_file_stem("gcl_resource_string", relative, archive_entry.name),
                        relative, "gcl_resource_string", item.data,
                        text_profile=text_profile,
                        structural_english=non_english_resources is not None,
                        archive_entry=archive_entry.name, resource_index=resource.index,
                        string_index=item.index, gcl_encoding=True,
                    )
            # Procedure strings are script arguments, stage IDs, file names, and
            # developer diagnostics. User-facing strings live in GCX resources.
        count = len(records) - before
        _log(log, f"[{stage_number:03}/{len(stage_files):03}] {zar_path.parent.name}: {count:,}")

    shared_line_count, shared_target_count = _merge_duplicate_lines(groups, records)
    groups = {name: lines for name, lines in groups.items() if lines}
    if shared_line_count:
        _log(
            log,
            f"Merged {shared_target_count:,} physical records into "
            f"{shared_line_count:,} shared editable lines",
        )

    for stem, lines in sorted(groups.items()):
        write_text_lines(texts_dir / f"{stem}.txt", lines)
    legacy_character_map = texts_dir / "character_map.json"
    if legacy_character_map.is_file():
        legacy_character_map.unlink()
    write_json(
        manifest_path,
        {
            "format": "mgs-portable-ops-text-manifest",
            "format_version": 11,
            "tool": APP_NAME,
            "tool_version": APP_VERSION,
            "text_profile": text_profile,
            "game_region": detect_game_region(usrdir),
            "source_files": source_hashes,
            "line_count": len(records),
            "editable_line_count": sum(len(lines) for lines in groups.values()),
            "shared_stage_line_count": shared_line_count,
            "shared_stage_target_count": shared_target_count,
            "text_files": {f"{name}.txt": len(lines) for name, lines in sorted(groups.items())},
            "entries": records,
        },
    )
    index_rows = []
    for record in records:
        index_rows.append([
            Path(str(record["text_file"])).name,
            str(record["line_number"]),
            str(record.get("kind", "")),
            str(record.get("source_file", "")),
            str(record.get("archive_entry", "")),
            str(record.get("resource_index", "")),
            str(record.get("dar_entry", "")),
            str(record.get("subtitle_index", "")),
        ])
    index_path = output_root / TEXT_LINE_INDEX_NAME
    with index_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["text_file", "line", "kind", "source_file", "archive_entry",
                         "resource_index", "dar_entry", "subtitle_index"])
        writer.writerows(index_rows)

    editable_line_count = sum(len(lines) for lines in groups.values())
    _log(
        log,
        f"Text export complete: {editable_line_count:,} editable lines "
        f"({len(records):,} physical records) in {len(groups):,} files",
    )
    return texts_dir, manifest_path, editable_line_count


def _load_manifest_lines(
    manifest: dict,
    export_root: Path,
    manifest_path: Path | None = None,
) -> tuple[list[dict], dict[str, list[str]]]:
    if manifest.get("format") != "mgs-portable-ops-text-manifest" or manifest.get("format_version") not in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        raise ToolkitError("This is not a current Portable Ops text manifest.")
    entries = manifest.get("entries")
    counts = manifest.get("text_files")
    if not isinstance(entries, list) or not isinstance(counts, dict):
        raise ToolkitError("The text manifest has no valid entry table.")
    files: dict[str, list[str]] = {}
    version = int(manifest.get("format_version", 0))
    export_root = export_root.resolve()
    candidate_roots = [export_root]
    if export_root.name.casefold() == "texts":
        candidate_roots.append(export_root.parent)
    if manifest_path is not None:
        candidate_roots.append(manifest_path.resolve().parent)
    text_root = next(
        (
            candidate
            for candidate in candidate_roots
            if all((candidate / "texts" / str(name)).is_file() for name in counts)
        ),
        export_root,
    )
    for name, expected_count in counts.items():
        relative = Path("texts") / name
        lines = read_text_lines(text_root / relative)
        if len(lines) != expected_count:
            raise ToolkitError(f"{name} has {len(lines):,} lines; the manifest expects {expected_count:,}.")
        files[name] = lines
    for expected_id, entry in enumerate(entries, 1):
        if entry.get("id") != expected_id:
            raise ToolkitError(f"Manifest entry order is invalid at ID {expected_id}.")
        raw_name = str(entry.get("text_file", ""))
        name = Path(raw_name).name if version >= 3 else raw_name
        line = entry.get("line_number") if version >= 3 else entry.get("line")
        if name not in files or not isinstance(line, int) or not (1 <= line <= len(files[name])):
            raise ToolkitError(f"Manifest text location is invalid at ID {expected_id}.")
    return entries, files


def import_text(
    source: Path,
    manifest_path: Path,
    export_root: Path,
    destination: Path,
    log: Log | None = None,
) -> tuple[Path, Path, int, int]:
    source_usrdir, source_root = resolve_usrdir(source)
    manifest = read_json(manifest_path)
    entries, files = _load_manifest_lines(manifest, export_root, manifest_path)
    codec_path = source_usrdir / "codec.dat"
    codec_data = codec_path.read_bytes()
    codec_blocks = parse_codec(codec_data)
    text_profile = _detect_text_profile(codec_data, len(codec_blocks))
    manifest_profile = manifest.get("text_profile")
    if manifest_profile is not None and manifest_profile != text_profile:
        raise ToolkitError("The text export and selected game use different regional text encodings.")
    manifest_region = manifest.get("game_region")
    if manifest_region is not None:
        source_region = detect_game_region(source_usrdir)
        if source_region != "unknown" and manifest_region != source_region:
            raise ToolkitError(
                f"This text export was made from the {manifest_region} release, but the selected game "
                f"is the {source_region} release. Export the text from the same game you are rebuilding."
            )
    font_widths = _load_font_widths(source_usrdir)
    character_map = {
        character: bytes.fromhex(encoded)
        for character, encoded in default_character_map(text_profile)["mappings"].items()
    }

    changed_entries: list[tuple[dict, bytes | list[bytes]]] = []
    encode_failures: list[str] = []
    boot_unrenderable: dict[str, int] = {}
    for entry in entries:
        text_name = Path(str(entry["text_file"])).name
        line_number = int(entry.get("line_number", entry.get("line", 0)))
        line = files[text_name][line_number - 1]
        if line == entry.get("original_text"):
            continue
        laid_out = _apply_text_layout(entry, line, font_widths)
        laid_out_lines = laid_out if isinstance(laid_out, list) else [laid_out]
        rebuilt_lines = [
            str(entry.get("control_prefix", ""))
            + laid_out_line
            + str(entry.get("control_suffix", ""))
            for laid_out_line in laid_out_lines
        ]
        kind = str(entry["kind"])
        encoded_lines: list[bytes] = []
        try:
            for rebuilt_line in rebuilt_lines:
                if kind == "boot_ui_text":
                    boot_unrenderable.update(boot_unrenderable_cells(rebuilt_line))
                    encoded = text_to_boot_bytes(rebuilt_line, boot_unrenderable)
                elif kind == "utf8_resource_string":
                    encoded = text_to_utf8_bytes(rebuilt_line)
                    encoded += base64.b64decode(entry.get("trailing_base64", ""), validate=True)
                elif kind == "gcl_resource_string" or kind == "gcl_procedure_string":
                    encoded = text_to_gcl_bytes(rebuilt_line, character_map)
                else:
                    encoded = text_to_bytes(rebuilt_line, character_map)
                    if kind == "gcx_string":
                        encoded += base64.b64decode(entry.get("trailing_base64", ""), validate=True)
                encoded_lines.append(encoded)
        except ToolkitError as exc:
            encode_failures.append(f"{text_name}:{line_number} ({kind}): {exc}")
            continue
        encoded_value: bytes | list[bytes] = (
            encoded_lines if kind == "cap_subtitle" else encoded_lines[0]
        )
        changed_entries.append((entry, encoded_value))
    if encode_failures:
        shown = "\n".join(f"  - {item}" for item in encode_failures[:25])
        extra = f"\n  ... and {len(encode_failures) - 25} more." if len(encode_failures) > 25 else ""
        raise ToolkitError(f"Could not encode {len(encode_failures)} text lines:\n{shown}{extra}")
    changed_lines = {
        (Path(str(entry["text_file"])).name, int(entry.get("line_number", entry.get("line", 0))))
        for entry, _payload in changed_entries
    }
    _log(
        log,
        f"Validated {sum(len(lines) for lines in files.values()):,} editable lines; "
        f"{len(changed_lines):,} changed ({len(changed_entries):,} physical records)",
    )
    if boot_unrenderable:
        detail = ', '.join(f"{ch!r} (1F{code:02X})" for ch, code in sorted(boot_unrenderable.items()))
        raise ToolkitError(f"System font cannot render these characters: {detail}. Use a supported font slot.")

    rebuilt_root, rebuilt_usrdir = prepare_rebuild_root(source, destination)
    codec_changes: dict[int, dict[int, bytes]] = defaultdict(dict)
    boot_changes: dict[int, bytes] = {}
    stage_changes: dict[str, list[tuple[dict, bytes | list[bytes]]]] = defaultdict(list)
    for entry, payload in changed_entries:
        if entry["kind"] == "codec_text":
            if isinstance(payload, list):
                raise ToolkitError("A codec text unexpectedly produced multiple subtitle pages.")
            codec_changes[int(entry["block_index"])][int(entry["text_index"])] = payload
        elif entry["kind"] == "boot_ui_text":
            if isinstance(payload, list):
                raise ToolkitError("A BOOT.BIN text unexpectedly produced multiple pages.")
            boot_changes[int(entry["boot_text_index"])] = payload
        else:
            stage_changes[str(entry["source_file"])].append((entry, payload))

    if sha256_bytes(codec_data) != manifest.get("source_files", {}).get("codec.dat"):
        raise ToolkitError("The selected game does not match the codec.dat used for this export.")
    if codec_changes:
        # Rebuild every block at its actual translated size, as in the working
        # backup. Missing direct script references do not establish fixed space.
        # build_codec verifies every replacement; no translation is discarded.
        from gcl import codec_reference_capacities
        source_starts = codec_file_starts(codec_data)
        source_lookup = {offset:i for i,offset in enumerate(source_starts)}
        source_need = {i:((source_starts[i+1] if i+1<len(source_starts) else len(codec_data))-offset+2047)//2048
                       for i,offset in enumerate(source_starts)}
        read_capacities: dict[int, int] = {}
        for stage_path in sorted((source_usrdir/'stage').glob('*/_zar')):
            for item in parse_zar(stage_path.read_bytes()):
                if not item.name.lower().endswith(('.gcx','.fcx')):
                    continue
                script = parse_gcx(item.data)
                for index,capacity in codec_reference_capacities(script.prefix,script.script_data,source_lookup,source_need).items():
                    read_capacities[index] = max(read_capacities.get(index,0),capacity)
        (rebuilt_usrdir / "codec.dat").write_bytes(build_codec(codec_blocks, codec_changes, read_capacities=read_capacities))
        _log(log, f"Rebuilt codec.dat with {sum(len(value) for value in codec_changes.values()):,} changes")

    boot_path = source_root / Path(BOOT_SOURCE_FILE)
    if boot_path.is_file():
        boot_data = boot_path.read_bytes()
        expected_boot_hash = manifest.get("source_files", {}).get(BOOT_SOURCE_FILE)
        if expected_boot_hash and sha256_bytes(boot_data) != expected_boot_hash:
            raise ToolkitError("The selected game does not match the BOOT.BIN used for this export.")
        if boot_changes:
            rebuilt_boot = build_boot_ui_table(parse_boot_ui_table(boot_data), boot_changes)
        else:
            rebuilt_boot = boot_data
        rebuilt_boot = apply_identity_glyph_mapping(rebuilt_boot)
        rebuilt_boot_path = rebuilt_root / Path(BOOT_SOURCE_FILE)
        rebuilt_boot_path.parent.mkdir(parents=True, exist_ok=True)
        rebuilt_boot_path.write_bytes(rebuilt_boot)
        rebuilt_eboot_path = rebuilt_root / "PSP_GAME/SYSDIR/EBOOT.BIN"
        rebuilt_eboot_path.parent.mkdir(parents=True, exist_ok=True)
        rebuilt_eboot_path.write_bytes(rebuilt_boot)
        _log(log, f"Rebuilt BOOT.BIN and EBOOT.BIN with {len(boot_changes):,} system UI changes; retail decoder preserved")

    codec_layout = None
    if codec_changes:
        old_starts = codec_file_starts(codec_data)
        old_sizes = [
            (old_starts[index + 1] if index + 1 < len(old_starts) else len(codec_data)) - start
            for index, start in enumerate(old_starts)
        ]
        old_start_to_index = {start: index for index, start in enumerate(old_starts)}
        old_need = {index: (size + 2047) // 2048 for index, size in enumerate(old_sizes)}
        new_starts, new_sizes = codec_rebuilt_layout(codec_blocks, codec_changes)
        write_json(rebuilt_root.parent/'codec_layout.json', {
            'format': 'mgs-po-expanded-codec-layout',
            'original_starts': old_starts, 'new_starts': new_starts,
            'new_sizes': new_sizes, 'kept_original_capacity': {},
        })
        codec_layout = (old_start_to_index, old_need, new_starts, new_sizes)
        relocated_blocks = sum(1 for old, new in zip(old_starts, new_starts) if old != new)
        grown_blocks = sum(1 for old, new in zip(old_sizes, new_sizes) if old != new)
        _log(log, f"Codec layout: {grown_blocks:,} blocks resized, {relocated_blocks:,} blocks moved")

    stage_files: dict[str, list[tuple[dict, bytes | list[bytes]]]] = defaultdict(list)
    for source_file, changes in stage_changes.items():
        stage_files[source_file].extend(changes)
    if codec_layout is not None:
        for zar_path in sorted((source_usrdir / "stage").glob("*/_zar")):
            relative = zar_path.relative_to(source_usrdir).as_posix()
            if relative in NON_CODEC_STAGES:
                continue
            if relative not in stage_files:
                stage_files[relative] = []

    for source_file, changes in sorted(stage_files.items()):
        original_path = source_usrdir / Path(source_file)
        if not original_path.is_file():
            continue
        original_data = original_path.read_bytes()
        expected_hash = manifest.get("source_files", {}).get(source_file)
        if expected_hash and sha256_bytes(original_data) != expected_hash:
            raise ToolkitError(f"The selected game does not match exported file {source_file}.")
        by_name: dict[str, list[tuple[dict, bytes | list[bytes]]]] = defaultdict(list)
        for entry, payload in changes:
            by_name[str(entry["archive_entry"])].append((entry, payload))
        rebuilt_entries: list[NamedEntry] = []
        stage_reloc_total = 0
        for archive_entry in parse_zar(original_data):
            file_changes = by_name.get(archive_entry.name)
            if not file_changes:
                if (
                    codec_layout is None
                    or source_file in NON_CODEC_STAGES
                    or Path(archive_entry.name).suffix.lower() not in (".gcx", ".fcx")
                ):
                    rebuilt_entries.append(archive_entry)
                    continue
                try:
                    parsed_empty = parse_gcx(archive_entry.data)
                except ToolkitError:
                    rebuilt_entries.append(archive_entry)
                    continue
                old_start_to_index, old_need, new_starts, new_sizes = codec_layout
                try:
                    patched_script, relocated = patch_gcx_codec_refs(
                        parsed_empty.prefix,
                        parsed_empty.script_data,
                        old_start_to_index,
                        old_need,
                        new_starts,
                        new_sizes,
                    )
                except ToolkitError as exc:
                    raise ToolkitError(f"Codec relocation failed in {source_file}/{archive_entry.name}: {exc}") from exc
                if not relocated:
                    rebuilt_entries.append(archive_entry)
                    continue
                rebuilt_entries.append(NamedEntry(
                    archive_entry.name,
                    build_gcx(parsed_empty, None, script_data=patched_script),
                    archive_entry.hash_value,
                ))
                stage_reloc_total += relocated
                continue
            if file_changes[0][0]["kind"] == "cap_subtitle":
                dar_entries = parse_dar(archive_entry.data)
                by_dar: dict[int, list[tuple[dict, bytes | list[bytes]]]] = defaultdict(list)
                for entry, payload in file_changes:
                    by_dar[int(entry["dar_index"])].append((entry, payload))
                rebuilt_dar: list[NamedEntry] = []
                for dar_index, dar_entry in enumerate(dar_entries):
                    subtitle_changes = by_dar.get(dar_index)
                    if not subtitle_changes:
                        rebuilt_dar.append(dar_entry)
                        continue
                    cap = parse_cap(dar_entry.data)
                    replacements = {}
                    for entry, payload in subtitle_changes:
                        pages = payload if isinstance(payload, list) else [payload]
                        replacements[int(entry["subtitle_index"])] = [page[:-1] if page.endswith(b"\0") else page for page in pages]
                    rebuilt_dar.append(
                        NamedEntry(dar_entry.name, build_cap(cap, replacements), dar_entry.hash_value)
                    )
                rebuilt_entries.append(
                    NamedEntry(archive_entry.name, build_dar(rebuilt_dar), archive_entry.hash_value)
                )
                continue
            parsed = parse_gcx(archive_entry.data)
            resource_replacements: dict[int, bytes] = {}
            gcl_resource_changes: dict[int, dict[int, bytes]] = defaultdict(dict)
            procedure_changes: dict[tuple[str, int, int], bytes] = {}
            for entry, payload in file_changes:
                kind = entry["kind"]
                if isinstance(payload, list):
                    raise ToolkitError(f"A non-CAP text unexpectedly produced multiple pages: {kind}")
                if kind == "gcx_string":
                    resource_replacements[int(entry["resource_index"])] = payload
                elif kind == "utf8_resource_string":
                    resource_replacements[int(entry["resource_index"])] = payload
                elif kind == "gcl_resource_string":
                    gcl_resource_changes[int(entry["resource_index"])][int(entry["string_index"])] = payload
                elif kind == "gcl_procedure_string":
                    key = (str(entry["procedure_kind"]), int(entry["procedure_index"]), int(entry["string_index"]))
                    procedure_changes[key] = payload
                else:
                    raise ToolkitError(f"Unsupported text manifest kind: {kind}")
            for resource_index, replacements in gcl_resource_changes.items():
                resource_replacements[resource_index] = build_gcl_resource(
                    parsed.resources[resource_index].data, replacements
                )
            prefix, script_data = build_gcl_script(parsed.prefix, parsed.script_data, procedure_changes)
            if codec_layout is not None and source_file not in NON_CODEC_STAGES:
                old_start_to_index, old_need, new_starts, new_sizes = codec_layout
                try:
                    script_data, relocated = patch_gcx_codec_refs(
                        prefix, script_data, old_start_to_index, old_need, new_starts, new_sizes
                    )
                except ToolkitError as exc:
                    raise ToolkitError(
                        f"Codec relocation failed in {source_file}/{archive_entry.name}: {exc}"
                    ) from exc
                stage_reloc_total += relocated
            rebuilt_entries.append(NamedEntry(
                archive_entry.name,
                build_gcx(parsed, resource_replacements, prefix=prefix, script_data=script_data),
                archive_entry.hash_value,
            ))
        has_text_changes = bool(changes)
        if not has_text_changes and stage_reloc_total == 0:
            continue
        (rebuilt_usrdir / Path(source_file)).write_bytes(build_zar(rebuilt_entries))
        detail = f"{len(changes):,} text changes"
        if codec_layout is not None and stage_reloc_total:
            detail += f", {stage_reloc_total:,} codec references relocated"
        _log(log, f"Rebuilt {source_file} with {detail}")

    report_path = rebuilt_root.parent / TEXT_IMPORT_REPORT_NAME
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("line", "source_file", "kind", "status", "message"))
    for entry, _payload in changed_entries:
        writer.writerow((
            int(entry.get("line_number", entry.get("line", 0))),
            entry["source_file"], entry["kind"], "patched", "Rebuilt and verified without a fixed text capacity",
        ))
    report_path.write_text(stream.getvalue(), encoding="utf-8")
    # Clear a capacity report left by older versions when reusing an output folder.
    (report_path.parent/'codec_capacity_report.csv').write_text(
        'block_index,required_bytes,available_bytes,excess_bytes,project_lines\n', encoding='utf-8-sig')
    _log(log, f"Rebuild complete: {rebuilt_root}")
    return rebuilt_root, report_path, len(changed_lines), 0
