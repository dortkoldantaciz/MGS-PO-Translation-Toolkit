from __future__ import annotations

import shutil
import struct
from collections import defaultdict
from pathlib import Path
from typing import Callable

from archives import (
    NamedEntry,
    build_dar,
    build_qar,
    build_zar,
    parse_dar,
    parse_qar,
    parse_zar,
)
from common import (
    APP_NAME,
    APP_VERSION,
    ASSET_MANIFEST_NAME,
    ToolkitError,
    detect_game_region,
    prepare_rebuild_root,
    read_json,
    resolve_usrdir,
    safe_file_part,
    sha256_bytes,
    sha256_file,
    write_json,
)
from png import read_rgba_png
from font import export_resident_font, import_resident_font
from slot import build_slot, parse_slot, patch_stage_slot_page_codes
from txp import export_texture_png, import_texture_png, parse_txp, texture_rgba


Log = Callable[[str], None]
RESIDENT_FONT_NAMES = {"keepfont.row", "keepfont_mini.row"}
ITEM_NAME_FONT_TEXTURE = "fontprint.txp"
BOOT_FONT_TEXTURE = "db_font.txp"
# The main-menu atlas: three row-height sheets (384x48 / 320x48 / 256x48) in
# stage/init/cache.qar.  It was never exported, so the menu font could not be edited and
# accented menu labels stayed unfixable.
MENU_FONT_TEXTURE = "menu.txp"
FONT_MODE_TEXTURES = (ITEM_NAME_FONT_TEXTURE, BOOT_FONT_TEXTURE, MENU_FONT_TEXTURE)
MENU_SHEET_SHAPES = {(448, 64), (256, 48), (256, 40)}
MENU_FONT_MIN_ENTRIES = 80
MENU_FONT_MIN_SCORE = 0.6


def _la2_font_tables(la2: bytes) -> list[list[tuple[int, int, int, int, int, int, bytes]]]:
    try:
        offsets = struct.unpack_from("<12I", la2, 4)
    except struct.error:
        return []
    section = offsets[10]
    if not section or section + 4 > len(la2):
        return []
    tables: list[list[tuple[int, int, int, int, int, int, bytes]]] = []
    position = section + 4
    try:
        count = struct.unpack_from("<I", la2, section)[0]
    except struct.error:
        return []
    for _ in range(min(count, 64)):
        if position + 8 > len(la2):
            break
        _hash, entries, _size = struct.unpack_from("<IHH", la2, position)
        start = position + 8
        if entries > 2048 or start + 16 * entries > len(la2):
            break
        rows = []
        for index in range(entries):
            entry = start + 16 * index
            advance, height, u0, u1, v0, v1, key = struct.unpack_from("<HHHHHH4s", la2, entry)
            rows.append((advance, height, u0, u1, v0, v1, key))
        tables.append(rows)
        position = start + 16 * entries
    return tables


def _font_box_score(entries, width: int, height: int, ink: bytes) -> float:
    good = 0
    total = 0
    for _advance, _height, u0, u1, v0, v1, key in entries:
        label = key.rstrip(b"\0")
        if len(label) != 1 or not 0x21 <= label[0] < 0x7F or u1 <= u0:
            continue
        x0, x1 = round(u0 / 65536 * width), round(u1 / 65536 * width)
        y0, y1 = round(v0 / 65536 * height), round(v1 / 65536 * height)
        total += 1
        if x1 <= x0 or y1 <= y0 or x1 > width or y1 > height:
            continue
        inside = sum(ink[y * width + x] for y in range(y0, y1) for x in range(x0, x1))
        edge = sum(
            ink[y * width + x]
            for y in range(y0, y1)
            for x in (x0 - 1, x1)
            if 0 <= x < width
        )
        good += inside > 0 and edge <= (y1 - y0) // 4
    return good / total if total else 0.0


def _menu_sheet_indices(tables, textures) -> list[int]:
    """Every sheet these font tables can draw glyphs from.  A layout keeps one sheet per
    shape (heading / items / labels) and different text objects pick different ones, so all
    shapes that the tables address are exported, not only the best match."""
    tables = [rows for rows in tables if len(rows) >= MENU_FONT_MIN_ENTRIES]
    if not tables:
        return []
    selected: list[int] = []
    for texture in textures:
        if (texture.width, texture.height) not in MENU_SHEET_SHAPES:
            continue
        rgba = texture_rgba(texture)
        ink = bytes(
            1 if rgba[(y * texture.width + x) * 4 + 3] > 0x40 else 0
            for y in range(texture.height)
            for x in range(texture.width)
        )
        best = max(
            _font_box_score(rows, texture.width, texture.height, ink) for rows in tables
        )
        if best >= MENU_FONT_MIN_SCORE:
            selected.append(texture.index)
    return selected


def _log(log: Log | None, message: str) -> None:
    if log:
        log(message)


def _is_selected_asset(name: str, mode: str) -> bool:
    lower = name.lower()
    if lower.endswith(".txp"):
        if mode == "texture":
            return True
        if mode == "font":
            return lower in FONT_MODE_TEXTURES
        return False
    if lower in RESIDENT_FONT_NAMES:
        return mode == "font"
    return mode == "texture" and lower.endswith(".png")


def _font_sheet_selection(outer_entries) -> dict[tuple[int, int], set[int]]:
    """(qar outer index, txp inner index) -> sheet texture indices its layout font tables draw from"""
    la2_by_base: dict[str, bytes] = {}
    txp_locations: dict[str, tuple[int, int, bytes]] = {}
    for outer_index, entry in enumerate(outer_entries):
        lower = entry.name.lower()
        if lower.endswith(".dar"):
            try:
                inner_entries = parse_dar(entry.data)
            except ToolkitError:
                continue
            for inner in inner_entries:
                name = inner.name.lower()
                if name.startswith("lay_") and name.endswith(".la2"):
                    la2_by_base[inner.name[:-4].lower()] = inner.data
        elif lower.endswith(".qar"):
            try:
                inner_entries = parse_qar(entry.data)
            except ToolkitError:
                continue
            for inner_index, inner in enumerate(inner_entries):
                name = inner.name.lower()
                if name.startswith("lay_") and name.endswith("_0.txp"):
                    txp_locations[name[:-6]] = (outer_index, inner_index, inner.data)
    stage_tables = [
        rows
        for la2 in la2_by_base.values()
        for rows in _la2_font_tables(la2)
    ]
    if not stage_tables:
        return {}
    selection: dict[tuple[int, int], set[int]] = {}
    for base, (outer_index, inner_index, data) in txp_locations.items():
        la2 = la2_by_base.get(base)
        tables = _la2_font_tables(la2) if la2 is not None else stage_tables
        try:
            textures = parse_txp(data).textures
        except ToolkitError:
            continue
        indices = _menu_sheet_indices(tables, textures)
        if indices:
            selection[(outer_index, inner_index)] = set(indices)
    return selection


def _scan_assets(usrdir: Path, mode: str, log: Log | None = None) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    stage_files = sorted((usrdir / "stage").glob("*/_zar"), key=lambda path: path.parent.name.lower())
    for stage_number, zar_path in enumerate(stage_files, 1):
        zar_relative = zar_path.relative_to(usrdir).as_posix()
        outer_entries = parse_zar(zar_path.read_bytes())
        sheet_selection = _font_sheet_selection(outer_entries) if mode == "font" else {}
        found = 0
        for outer_index, outer_entry in enumerate(outer_entries):
            lower = outer_entry.name.lower()
            if lower.endswith(".qar"):
                inner_kind = "qar"
                inner_entries = parse_qar(outer_entry.data)
            elif lower.endswith(".dar"):
                inner_kind = "dar"
                inner_entries = parse_dar(outer_entry.data)
            else:
                continue
            for inner_index, inner_entry in enumerate(inner_entries):
                selection = sheet_selection.get((outer_index, inner_index))
                if not selection and not _is_selected_asset(inner_entry.name, mode):
                    continue
                digest = sha256_bytes(inner_entry.data)
                package = packages.setdefault(
                    digest,
                    {
                        "id": digest,
                        "kind": (
                            "txp" if inner_entry.name.lower().endswith(".txp")
                            else "resident_font" if inner_entry.name.lower() in RESIDENT_FONT_NAMES
                            else "png"
                        ),
                        "display_name": inner_entry.name,
                        "source_sha256": digest,
                        "occurrences": [],
                    },
                )
                package["occurrences"].append(
                    {
                        "zar_file": zar_relative,
                        "outer_index": outer_index,
                        "outer_entry": outer_entry.name,
                        "inner_kind": inner_kind,
                        "inner_index": inner_index,
                        "inner_entry": inner_entry.name,
                    }
                )
                selection = sheet_selection.get((outer_index, inner_index))
                if selection:
                    existing = package.get("texture_indices")
                    package["texture_indices"] = (
                        sorted(selection) if existing is None
                        else sorted(set(existing) | selection)
                    )
                found += 1
        if found:
            _log(log, f"[{stage_number:03}/{len(stage_files):03}] {zar_path.parent.name}: {found:,} asset references")
    if mode == "texture":
        slot_path = usrdir / "slot.dat"
        parsed_slot = parse_slot(slot_path.read_bytes())
        slot_references = 0
        for page in parsed_slot.pages:
            for asset in page.assets:
                if asset.extension_code != 0x14:
                    continue
                digest = sha256_bytes(asset.data)
                display_name = f"slot_p{page.index:03}_{asset.asset_id & 0xFFFFFF:06x}.txp"
                package = packages.setdefault(
                    digest,
                    {
                        "id": digest,
                        "kind": "txp",
                        "display_name": display_name,
                        "source_sha256": digest,
                        "occurrences": [],
                    },
                )
                package["occurrences"].append(
                    {
                        "slot_file": "slot.dat",
                        "inner_kind": "slot",
                        "page_index": page.index,
                        "asset_index": asset.index,
                        "asset_id": asset.asset_id,
                        "slot_offset": asset.absolute_offset,
                        "slot_size": len(asset.data),
                    }
                )
                slot_references += 1
        _log(log, f"slot.dat: {slot_references:,} TXP asset references")
    return packages


def _source_asset(usrdir: Path, occurrence: dict) -> bytes:
    if occurrence["inner_kind"] == "slot":
        slot_path = usrdir / occurrence["slot_file"]
        with slot_path.open("rb") as stream:
            stream.seek(int(occurrence["slot_offset"]))
            data = stream.read(int(occurrence["slot_size"]))
        if len(data) != int(occurrence["slot_size"]):
            raise ToolkitError("slot.dat asset range is truncated.")
        return data
    zar_path = usrdir / Path(occurrence["zar_file"])
    outer_entries = parse_zar(zar_path.read_bytes())
    outer_index = int(occurrence["outer_index"])
    if outer_index >= len(outer_entries):
        raise ToolkitError(f"Archive entry index changed in {occurrence['zar_file']}.")
    outer = outer_entries[outer_index]
    if outer.name != occurrence["outer_entry"]:
        raise ToolkitError(f"Archive entry order changed in {occurrence['zar_file']}.")
    inner_entries = parse_qar(outer.data) if occurrence["inner_kind"] == "qar" else parse_dar(outer.data)
    inner_index = int(occurrence["inner_index"])
    if inner_index >= len(inner_entries):
        raise ToolkitError(f"Nested archive entry index changed in {outer.name}.")
    inner = inner_entries[inner_index]
    if inner.name != occurrence["inner_entry"]:
        raise ToolkitError(f"Nested archive entry order changed in {outer.name}.")
    return inner.data


def export_assets(source: Path, output_root: Path, mode: str, log: Log | None = None) -> tuple[Path, int]:
    if mode not in ("texture", "font"):
        raise ToolkitError(f"Unknown asset mode: {mode}")
    usrdir, _game_root = resolve_usrdir(source)
    output_root = output_root.resolve()
    editable_dir = output_root / ("textures" if mode == "texture" else "fonts")
    if output_root.exists():
        shutil.rmtree(output_root)
    editable_dir.mkdir(parents=True)
    packages = _scan_assets(usrdir, mode, log)
    exported_count = 0
    shared_count = 0
    written_pixels: dict[tuple[int, int, str], Path] = {}
    manifest_packages: list[dict] = []
    for package_number, package in enumerate(sorted(packages.values(), key=lambda item: (item["display_name"].lower(), item["id"])), 1):
        source_data = _source_asset(usrdir, package["occurrences"][0])
        if sha256_bytes(source_data) != package["source_sha256"]:
            raise ToolkitError(f"Source changed while exporting {package['display_name']}.")
        folder_name = safe_file_part(Path(package["display_name"]).stem) + "_" + package["id"][:10]
        package["files"] = []
        if package["kind"] == "txp":
            txp = parse_txp(source_data)
            flat_font = mode == "font"
            wanted = package.get("texture_indices")
            for texture in txp.textures:
                if wanted is not None and texture.index not in wanted:
                    continue
                if flat_font:
                    relative = Path(f"{folder_name}_tex{texture.index:03}.png")
                else:
                    relative = Path(folder_name) / f"texture_{texture.index:03}.png"
                target = editable_dir / relative
                rgba = texture_rgba(texture)
                fingerprint = (texture.width, texture.height, sha256_bytes(rgba))
                if flat_font and fingerprint in written_pixels:
                    relative = written_pixels[fingerprint]
                    target = editable_dir / relative
                    shared_count += 1
                else:
                    export_texture_png(txp, texture.index, target)
                    if flat_font:
                        written_pixels[fingerprint] = relative
                package["files"].append(
                    {
                        "texture_index": texture.index,
                        "file": relative.as_posix(),
                        "width": texture.width,
                        "height": texture.height,
                        "export_sha256": sha256_file(target),
                    }
                )
                exported_count += 1
        elif package["kind"] == "resident_font":
            relative = Path("latin_" + folder_name + ".png")
            target = editable_dir / relative
            width, height, glyph_count = export_resident_font(source_data, target)
            package["files"].append(
                {
                    "file": relative.as_posix(),
                    "width": width,
                    "height": height,
                    "glyph_count": glyph_count,
                    "export_sha256": sha256_file(target),
                }
            )
            exported_count += 1
        else:
            relative = Path(folder_name + ".png")
            target = editable_dir / relative
            target.write_bytes(source_data)
            width, height, _rgba = read_rgba_png(target)
            package["files"].append(
                {
                    "file": relative.as_posix(),
                    "width": width,
                    "height": height,
                    "export_sha256": sha256_file(target),
                }
            )
            exported_count += 1
        manifest_packages.append(package)
        _log(log, f"[{package_number:03}/{len(packages):03}] {package['display_name']}: {len(package['files'])} PNG")
    manifest_path = output_root / ASSET_MANIFEST_NAME
    write_json(
        manifest_path,
        {
            "format": "mgs-portable-ops-asset-manifest",
            "format_version": 1,
            "tool": APP_NAME,
            "tool_version": APP_VERSION,
            "mode": mode,
            "game_region": detect_game_region(usrdir),
            "editable_folder": editable_dir.name,
            "asset_count": exported_count,
            "packages": manifest_packages,
        },
    )
    _log(log, f"{mode.title()} export complete: {exported_count:,} PNG files")
    if shared_count:
        _log(log, f"{shared_count:,} duplicate textures share a single PNG file")
    return manifest_path, exported_count


def _build_changed_packages(usrdir: Path, manifest: dict, editable_dir: Path) -> dict[str, bytes]:
    changed_payloads: dict[str, bytes] = {}
    for package in manifest.get("packages", []):
        changed_files = [
            file_entry
            for file_entry in package.get("files", [])
            if sha256_file(editable_dir / Path(file_entry["file"])) != file_entry["export_sha256"]
        ]
        if not changed_files:
            continue
        source_data = _source_asset(usrdir, package["occurrences"][0])
        if sha256_bytes(source_data) != package["source_sha256"]:
            raise ToolkitError(f"The selected game does not match exported asset {package['display_name']}.")
        if package["kind"] == "txp":
            txp = parse_txp(source_data)
            replacements = {
                int(file_entry["texture_index"]): editable_dir / Path(file_entry["file"])
                for file_entry in changed_files
            }
            changed_payloads[package["id"]] = import_texture_png(txp, replacements)
        elif package["kind"] == "resident_font":
            changed_payloads[package["id"]] = import_resident_font(
                source_data, editable_dir / Path(changed_files[0]["file"])
            )
        else:
            file_entry = changed_files[0]
            edited_path = editable_dir / Path(file_entry["file"])
            width, height, _rgba = read_rgba_png(edited_path)
            if (width, height) != (int(file_entry["width"]), int(file_entry["height"])):
                raise ToolkitError(
                    f"Texture canvas changed for {edited_path.name}: expected {file_entry['width']}x{file_entry['height']}, got {width}x{height}."
                )
            changed_payloads[package["id"]] = edited_path.read_bytes()
    return changed_payloads


def import_assets(
    source: Path,
    manifest_path: Path,
    editable_dir: Path,
    destination: Path,
    log: Log | None = None,
) -> tuple[Path, int, int]:
    source_usrdir, _source_root = resolve_usrdir(source)
    manifest = read_json(manifest_path)
    if manifest.get("format") != "mgs-portable-ops-asset-manifest":
        raise ToolkitError("This is not a Portable Ops asset manifest.")
    manifest_region = manifest.get("game_region")
    if manifest_region is not None:
        source_region = detect_game_region(source_usrdir)
        if source_region != "unknown" and manifest_region != source_region:
            raise ToolkitError(
                f"This {manifest.get('mode', 'asset')} export was made from the {manifest_region} release, "
                f"but the selected game is the {source_region} release. Export the assets from the same "
                f"game you are rebuilding."
            )
    editable_dir = editable_dir.resolve()
    changed_payloads = _build_changed_packages(source_usrdir, manifest, editable_dir)
    _log(log, f"Validated assets; {len(changed_payloads):,} packages changed")
    rebuilt_root, rebuilt_usrdir = prepare_rebuild_root(source, destination)
    occurrence_changes: dict[str, list[tuple[dict, bytes, str]]] = defaultdict(list)
    slot_changes: list[tuple[dict, bytes, str]] = []
    for package in manifest.get("packages", []):
        payload = changed_payloads.get(package["id"])
        if payload is None:
            continue
        for occurrence in package["occurrences"]:
            change = (occurrence, payload, package["source_sha256"])
            if occurrence["inner_kind"] == "slot":
                slot_changes.append(change)
            else:
                occurrence_changes[occurrence["zar_file"]].append(change)
    for zar_file, changes in sorted(occurrence_changes.items()):
        source_path = source_usrdir / Path(zar_file)
        rebuilt_path = rebuilt_usrdir / Path(zar_file)
        outer_entries = parse_zar(source_path.read_bytes())
        by_outer: dict[int, list[tuple[dict, bytes, str]]] = defaultdict(list)
        for change in changes:
            by_outer[int(change[0]["outer_index"])].append(change)
        new_outer: list[NamedEntry] = []
        for outer_index, outer_entry in enumerate(outer_entries):
            outer_changes = by_outer.get(outer_index)
            if not outer_changes:
                new_outer.append(outer_entry)
                continue
            inner_kind = outer_changes[0][0]["inner_kind"]
            inner_entries = parse_qar(outer_entry.data) if inner_kind == "qar" else parse_dar(outer_entry.data)
            by_inner = {int(item[0]["inner_index"]): item for item in outer_changes}
            new_inner: list[NamedEntry] = []
            for inner_index, inner_entry in enumerate(inner_entries):
                change = by_inner.get(inner_index)
                if change is None:
                    new_inner.append(inner_entry)
                    continue
                occurrence, payload, expected_hash = change
                if inner_entry.name != occurrence["inner_entry"] or sha256_bytes(inner_entry.data) != expected_hash:
                    raise ToolkitError(f"Source asset mismatch in {zar_file}/{outer_entry.name}.")
                new_inner.append(NamedEntry(inner_entry.name, payload, inner_entry.hash_value))
            nested_data = build_qar(new_inner) if inner_kind == "qar" else build_dar(new_inner)
            new_outer.append(NamedEntry(outer_entry.name, nested_data, outer_entry.hash_value))
        rebuilt_path.write_bytes(build_zar(new_outer))
        _log(log, f"Rebuilt {zar_file} with {len(changes):,} asset references")
    if slot_changes:
        source_slot_path = source_usrdir / "slot.dat"
        parsed_slot = parse_slot(source_slot_path.read_bytes())
        replacements: dict[tuple[int, int], bytes] = {}
        for occurrence, payload, expected_hash in slot_changes:
            page_index = int(occurrence["page_index"])
            asset_index = int(occurrence["asset_index"])
            if page_index >= len(parsed_slot.pages) or asset_index >= len(parsed_slot.pages[page_index].assets):
                raise ToolkitError("slot.dat asset table no longer matches the export manifest.")
            asset = parsed_slot.pages[page_index].assets[asset_index]
            if asset.asset_id != int(occurrence["asset_id"]) or sha256_bytes(asset.data) != expected_hash:
                raise ToolkitError("slot.dat asset no longer matches the export manifest.")
            replacements[(page_index, asset_index)] = payload
        rebuilt_slot, page_mapping = build_slot(parsed_slot, replacements)
        (rebuilt_usrdir / "slot.dat").write_bytes(rebuilt_slot)
        shifted_pages = sum(old != new for old, new in page_mapping.items())
        changed_stage_files, changed_page_codes = patch_stage_slot_page_codes(rebuilt_usrdir, page_mapping)
        _log(log, f"Rebuilt slot.dat with {len(slot_changes):,} asset references; {shifted_pages:,} pages relocated")
        _log(log, f"Updated {changed_page_codes:,} page references in {changed_stage_files:,} stage archives")
    _log(log, f"Rebuild complete: {rebuilt_root}")
    return rebuilt_root, len(changed_payloads), 0
