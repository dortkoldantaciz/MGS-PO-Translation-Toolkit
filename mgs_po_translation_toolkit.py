#!/usr/bin/env python
"""Launcher for Metal Gear Solid Portable Ops Translation Toolkit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


WORK_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
LIB_DIR = WORK_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from common import ASSET_MANIFEST_NAME, APP_NAME, TEXT_MANIFEST_NAME
from operations import export_mode, import_mode
from ui import ToolkitApp, run_self_test


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--export-text", nargs=2, metavar=("GAME_FOLDER", "OUTPUT_FOLDER"))
    parser.add_argument("--import-text", nargs=3, metavar=("GAME_FOLDER", "EXPORT_FOLDER", "REBUILD_FOLDER"))
    parser.add_argument("--export-assets", nargs=3, metavar=("MODE", "GAME_FOLDER", "OUTPUT_FOLDER"))
    parser.add_argument("--import-assets", nargs=4, metavar=("MODE", "GAME_FOLDER", "EXPORT_FOLDER", "REBUILD_FOLDER"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ui-smoke", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test(args.ui_smoke)
        return 0
    if args.export_text:
        count = export_mode(Path(args.export_text[0]), Path(args.export_text[1]), "text", print)
        print(f"Exported {count:,} lines\n{args.export_text[1]}")
        return 0
    if args.import_text:
        source, export_folder, rebuild = map(Path, args.import_text)
        result = import_mode(source, export_folder / TEXT_MANIFEST_NAME, export_folder, rebuild, "text", print)
        produced, report, changed, skipped = result
        print(f"Rebuilt {changed:,} changed lines; skipped {skipped:,}\n{produced}\n{report}")
        return 0
    if args.export_assets:
        mode, source, output = args.export_assets
        count = export_mode(Path(source), Path(output), mode, print)
        print(f"Exported {count:,} {mode} PNG files\n{output}")
        return 0
    if args.import_assets:
        mode, source, export_folder, rebuild = args.import_assets
        export_root = Path(export_folder)
        produced, changed, skipped = import_mode(
            Path(source), export_root / ASSET_MANIFEST_NAME, export_root, Path(rebuild), mode, print
        )
        print(f"Rebuilt {changed:,} changed {mode} packages; skipped {skipped:,}\n{produced}")
        return 0
    ToolkitApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
