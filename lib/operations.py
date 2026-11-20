from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

from assets import export_assets, import_assets
from common import ASSET_MANIFEST_NAME, TEXT_IMPORT_REPORT_NAME, TEXT_MANIFEST_NAME, ToolkitError
from disc import PspIso
from text import export_text, import_text


Log = Callable[[str], None]


def _copy_text_reports(report: Path, destination: Path) -> None:
    shutil.copyfile(report, destination / TEXT_IMPORT_REPORT_NAME)
    for name in ('codec_layout.json', 'codec_capacity_report.csv'):
        source = report.parent / name
        if source.is_file():
            shutil.copyfile(source, destination / name)


@contextmanager
def materialized_source(source: Path, log: Log | None = None) -> Iterator[Path]:
    source = source.resolve()
    if source.is_dir():
        yield source
        return
    if source.suffix.lower() != ".iso":
        raise ToolkitError("Select a Portable Ops ISO or an extracted game folder.")
    with tempfile.TemporaryDirectory(prefix="mgspo_iso_") as temporary:
        if log:
            log("Reading Portable Ops game files from the source ISO")
        root = Path(temporary) / source.stem
        PspIso(source).extract_game(root)
        yield root


def _collect_iso_replacements(original_root: Path, rebuilt_root: Path) -> dict[str, bytes]:
    rebuilt_game = rebuilt_root / "PSP_GAME"
    replacements: dict[str, bytes] = {}
    for rebuilt in rebuilt_game.rglob("*"):
        if not rebuilt.is_file():
            continue
        relative = rebuilt.relative_to(rebuilt_root).as_posix()
        original = original_root / Path(relative)
        payload = rebuilt.read_bytes()
        if not original.is_file() or original.read_bytes() != payload:
            replacements[relative] = payload
    return replacements


def export_mode(source: Path, output: Path, mode: str, log: Log | None = None) -> int:
    with materialized_source(source, log) as game_root:
        if mode == "text":
            _texts, _manifest, count = export_text(game_root, output, log)
        else:
            _manifest, count = export_assets(game_root, output, mode, log)
    return count


def _directory_import(
    source: Path,
    manifest_path: Path,
    export_root: Path,
    destination: Path,
    mode: str,
    log: Log | None,
) -> tuple:
    if mode == "text":
        return import_text(
            source,
            manifest_path,
            export_root,
            destination,
            log,
        )
    return import_assets(
        source,
        manifest_path,
        export_root / ("textures" if mode == "texture" else "fonts"),
        destination,
        log,
    )


def import_mode(
    source: Path,
    manifest_path: Path,
    export_root: Path,
    destination: Path,
    mode: str,
    log: Log | None = None,
) -> tuple:
    source = source.resolve()
    if source.is_dir():
        return _directory_import(source, manifest_path, export_root, destination, mode, log)
    if source.suffix.lower() != ".iso":
        raise ToolkitError("Select a Portable Ops ISO or an extracted game folder.")
    if destination.suffix.lower() != ".iso":
        destination = destination / f"{source.stem}.{mode}.patched.iso"
    with materialized_source(source, log) as game_root:
        with tempfile.TemporaryDirectory(prefix="mgspo_rebuild_") as temporary:
            result = _directory_import(
                game_root, manifest_path, export_root, Path(temporary) / "output", mode, log
            )
            produced = result[0]
            changed = int(result[-2] if mode == "text" else result[1])
            skipped = int(result[-1] if mode == "text" else result[2])
            replacements = _collect_iso_replacements(game_root, produced)
            if log:
                log(f"Writing {len(replacements):,} rebuilt game files to a new ISO")
            PspIso(source).rebuild(destination, replacements)
            if mode == "text":
                report_path = destination.parent / TEXT_IMPORT_REPORT_NAME
                _copy_text_reports(result[1], destination.parent)
                return destination, report_path, changed, skipped
    return destination, changed, skipped


def export_all(
    source: Path,
    output_root: Path,
    modes: Iterable[str],
    log: Log | None = None,
) -> dict[str, int]:
    selected = [mode for mode in ("text", "texture", "font") if mode in set(modes)]
    if not selected:
        raise ToolkitError("Select at least one export type.")
    output_root.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with materialized_source(source, log) as game_root:
        for mode in selected:
            target = output_root / f"{mode}_export"
            if mode == "text":
                _texts, _manifest, count = export_text(game_root, target, log)
            else:
                _manifest, count = export_assets(game_root, target, mode, log)
            counts[mode] = count
    return counts


def detect_import_modes(export_root: Path) -> list[str]:
    modes: list[str] = []
    if (export_root / "text_export" / TEXT_MANIFEST_NAME).is_file():
        modes.append("text")
    for mode in ("texture", "font"):
        if (export_root / f"{mode}_export" / ASSET_MANIFEST_NAME).is_file():
            modes.append(mode)
    return modes


def import_all(
    source: Path,
    export_root: Path,
    destination: Path,
    log: Log | None = None,
) -> tuple[Path, dict[str, int]]:
    modes = detect_import_modes(export_root)
    if not modes:
        raise ToolkitError("No Portable Ops text, texture, or font manifests were found.")
    source = source.resolve()
    source_iso = source if source.is_file() else None
    with materialized_source(source, log) as materialized:
        current = materialized
        temporary_roots: list[Path] = []
        changed: dict[str, int] = {}
        text_report: Path | None = None
        temporary = tempfile.TemporaryDirectory(prefix="mgspo_all_")
        try:
            temp_root = Path(temporary.name)
            for index, mode in enumerate(modes):
                step = temp_root / f"step_{index:02}"
                mode_root = export_root / f"{mode}_export"
                manifest_name = TEXT_MANIFEST_NAME if mode == "text" else ASSET_MANIFEST_NAME
                result = _directory_import(current, mode_root / manifest_name, mode_root, step, mode, log)
                if mode == "text":
                    text_report = result[1]
                produced = result[0]
                count = int(result[-2] if mode == "text" else result[1])
                temporary_roots.append(produced)
                current = produced
                changed[mode] = count
            if source_iso is not None:
                if destination.suffix.lower() != ".iso":
                    destination = destination / f"{source_iso.stem}.all.patched.iso"
                replacements = _collect_iso_replacements(materialized, current)
                PspIso(source_iso).rebuild(destination, replacements)
                produced_final = destination
            else:
                if destination.exists():
                    raise ToolkitError("The All rebuild destination must not already exist.")
                shutil.copytree(current, destination)
                produced_final = destination
            if text_report is not None:
                _copy_text_reports(text_report, produced_final.parent)
        finally:
            temporary.cleanup()
    return produced_final, changed
