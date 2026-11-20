from __future__ import annotations

import tempfile
import struct
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from archives import NamedEntry, build_dar, build_qar, parse_dar, parse_qar
from common import (
    ASSET_MANIFEST_NAME,
    APP_NAME,
    TEXT_MANIFEST_NAME,
    ToolkitError,
    WORK_DIR,
    bytes_to_text,
    default_character_map,
    text_to_bytes,
    text_to_utf8_bytes,
    utf8_bytes_to_text,
)
from codec import CodecBlock, CodecText, build_codec, parse_codec
from executable import BOOT_TEXT_TO_ACCENT_BYTES, boot_bytes_to_text, decode_glyph_cell, text_to_boot_bytes
from operations import export_all, export_mode, import_all, import_mode
from png import read_rgba_png, write_rgba_png


class ToolkitApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("820x860")
        self.minsize(760, 860)
        self.configure(background="#f6f7f9")
        self.option_add("*takeFocus", 0)
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(WORK_DIR / "output"))
        self.all_export_var = tk.StringVar()
        self.mode_manifest_vars = {mode: tk.StringVar() for mode in ("text", "texture", "font")}
        self.mode_export_vars = {mode: tk.StringVar() for mode in ("text", "texture", "font")}
        self.mode_vars = {mode: tk.BooleanVar(value=True) for mode in ("text", "texture", "font")}
        self._build_style()
        self._build_ui()
        self.after(0, self._center)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background="#f6f7f9", foreground="#161a22", focuscolor="#ffffff")
        style.configure("TFrame", background="#f6f7f9")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f6f7f9")
        style.configure("Panel.TLabel", background="#ffffff")
        style.configure("TEntry", padding=(8, 7), fieldbackground="#ffffff", bordercolor="#d9dee7")
        style.map("TEntry", bordercolor=[("focus", "#d9dee7")], lightcolor=[("focus", "#d9dee7")])
        style.configure("TButton", padding=(14, 8), background="#ffffff", bordercolor="#d9dee7")
        style.map("TButton", background=[("active", "#f1f5f9"), ("focus", "#ffffff")], bordercolor=[("focus", "#d9dee7")])
        style.configure("Accent.TButton", background="#202632", foreground="#ffffff", bordercolor="#202632")
        style.map("Accent.TButton", background=[("active", "#111827"), ("focus", "#202632")])
        style.configure("TNotebook", background="#f6f7f9", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), background="#eef1f5", borderwidth=1, bordercolor="#cfd6e1")
        style.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("active", "#f8fafc")])
        style.configure("Horizontal.TProgressbar", background="#202632", troughcolor="#e9edf5")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=20)
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 17, "bold")).pack(side="left")
        ttk.Label(header, text="Made by d" + chr(0x00F6) + "rtkoldantaciz - v1", foreground="#657083").pack(side="right", pady=(6, 0))
        self.tabs = ttk.Notebook(root, takefocus=False)
        self.tabs.pack(fill="both", expand=True)
        self._build_all_tab()
        self._build_text_tab()
        self._build_asset_tab("Texture", "texture")
        self._build_asset_tab("Font", "font")
        log_panel = ttk.Frame(root, style="Panel.TFrame", padding=10)
        log_panel.pack(fill="both", pady=(12, 0))
        ttk.Label(log_panel, text="Log", style="Panel.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.progress = ttk.Progressbar(log_panel, maximum=100)
        self.progress.pack(fill="x", pady=(6, 6))
        self.log_box = tk.Text(log_panel, height=13, borderwidth=0, background="#fbfcff", wrap="word", takefocus=False)
        self.log_box.pack(fill="both", expand=True)

    def _path_row(self, parent: ttk.Frame, label: str, variable: tk.StringVar, command: Callable[[], None]) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=24, style="Panel.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=variable, takefocus=False).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row, text="Select", takefocus=False, command=command).pack(side="left")

    def _operation_panel(self, parent: ttk.Frame, export_var: tk.StringVar | None = None) -> ttk.Frame:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        panel.pack(fill="x", pady=(0, 14))
        self._path_row(panel, "Game ISO / Folder", self.source_var, self._browse_source)
        if export_var is not None:
            self._path_row(panel, "Export Root Folder", export_var, lambda: self._browse_folder(export_var))
        self._path_row(panel, "Output Folder / ISO", self.output_var, lambda: self._browse_folder(self.output_var))
        return panel

    def _build_all_tab(self) -> None:
        outer = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(outer, text="All")
        notebook = ttk.Notebook(outer, takefocus=False)
        notebook.pack(fill="both", expand=True)
        export_tab = ttk.Frame(notebook, padding=14)
        notebook.add(export_tab, text="Export")
        self._operation_panel(export_tab)
        options = ttk.Frame(export_tab, style="Panel.TFrame", padding=14)
        options.pack(fill="x", pady=(0, 14))
        for mode in ("text", "texture", "font"):
            ttk.Checkbutton(options, text=mode.title(), variable=self.mode_vars[mode], takefocus=False).pack(side="left", padx=(0, 20))
        ttk.Button(export_tab, text="Export Selected Types", style="Accent.TButton", takefocus=False,
                   command=lambda: self._run_threaded(self._export_all)).pack(anchor="w")
        import_tab = ttk.Frame(notebook, padding=14)
        notebook.add(import_tab, text="Import")
        self._operation_panel(import_tab, self.all_export_var)
        ttk.Button(import_tab, text="Import Detected Exports", style="Accent.TButton", takefocus=False,
                   command=lambda: self._run_threaded(self._import_all)).pack(anchor="w")

    def _build_text_tab(self) -> None:
        self._build_mode_tab("Text", "text")

    def _build_asset_tab(self, label: str, mode: str) -> None:
        self._build_mode_tab(label, mode)

    def _build_mode_tab(self, label: str, mode: str) -> None:
        outer = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(outer, text=label)
        notebook = ttk.Notebook(outer, takefocus=False)
        notebook.pack(fill="both", expand=True)
        export_tab = ttk.Frame(notebook, padding=14)
        notebook.add(export_tab, text="Export")
        self._operation_panel(export_tab)
        ttk.Button(export_tab, text=f"Export {label}", style="Accent.TButton", takefocus=False,
                   command=lambda: self._run_threaded(lambda: self._export_mode(mode))).pack(anchor="w")
        import_tab = ttk.Frame(notebook, padding=14)
        notebook.add(import_tab, text="Import")
        self._operation_panel(import_tab)
        manifest_label = "Text Manifest" if mode == "text" else f"{label} Manifest"
        export_label = "Text Export Folder" if mode == "text" else f"{label} Export Folder"
        self._path_row(
            import_tab, manifest_label, self.mode_manifest_vars[mode],
            lambda m=mode: self._browse_manifest(self.mode_manifest_vars[m]),
        )
        self._path_row(
            import_tab, export_label, self.mode_export_vars[mode],
            lambda m=mode: self._browse_folder(self.mode_export_vars[m]),
        )
        ttk.Button(import_tab, text=f"Import {label}", style="Accent.TButton", takefocus=False,
                   command=lambda: self._run_threaded(lambda: self._import_mode(mode))).pack(anchor="w", pady=(12, 0))

    def _browse_source(self) -> None:
        selected = filedialog.askopenfilename(parent=self, filetypes=[("PSP ISO", "*.iso"), ("All files", "*.*")])
        if not selected:
            selected = filedialog.askdirectory(parent=self, title="Select Extracted Portable Ops Folder")
        if selected:
            self.source_var.set(selected)

    def _browse_manifest(self, variable: tk.StringVar) -> None:
        selected = filedialog.askopenfilename(parent=self, filetypes=[("JSON manifest", "*.json"), ("All files", "*.*")])
        if selected:
            variable.set(selected)

    def _browse_folder(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(parent=self)
        if selected:
            variable.set(selected)

    def _required(self, variable: tk.StringVar, label: str) -> Path:
        value = variable.get().strip()
        if not value:
            raise ToolkitError(f"Select {label} first.")
        return Path(value)

    def _source(self) -> Path:
        path = self._required(self.source_var, "a game ISO or folder")
        if not path.exists():
            raise ToolkitError(f"Source path does not exist: {path}")
        return path

    def _output(self) -> Path:
        return self._required(self.output_var, "an output folder or ISO")

    def _export_mode(self, mode: str) -> None:
        root = self._output() / f"{mode}_export"
        count = export_mode(self._source(), root, mode, self._log)
        self.mode_export_vars[mode].set(str(root))
        manifest_name = TEXT_MANIFEST_NAME if mode == "text" else ASSET_MANIFEST_NAME
        self.mode_manifest_vars[mode].set(str(root / manifest_name))
        self.after(0, lambda: messagebox.showinfo(APP_NAME, f"Exported {count:,} {mode} items.", parent=self))

    def _import_mode(self, mode: str) -> None:
        result = import_mode(
            self._source(), self._required(self.mode_manifest_vars[mode], f"the {mode} manifest"),
            self._required(self.mode_export_vars[mode], f"the {mode} export folder"),
            self._output(), mode, self._log,
        )
        produced = result[0]
        changed = int(result[-2] if mode == "text" else result[1])
        skipped = int(result[-1])
        self._log(f"Output: {produced}")
        if mode == "text":
            self._log(f"Report: {result[1]}")
        self.after(0, lambda: messagebox.showinfo(APP_NAME, f"Imported {changed:,} changed {mode} items; skipped {skipped:,}.", parent=self))

    def _export_all(self) -> None:
        modes = [mode for mode, variable in self.mode_vars.items() if variable.get()]
        counts = export_all(self._source(), self._output(), modes, self._log)
        self.all_export_var.set(str(self._output()))
        for mode in modes:
            mode_root = self._output() / f"{mode}_export"
            self.mode_export_vars[mode].set(str(mode_root))
            self.mode_manifest_vars[mode].set(str(mode_root / (TEXT_MANIFEST_NAME if mode == "text" else ASSET_MANIFEST_NAME)))
        summary = ", ".join(f"{mode}: {count:,}" for mode, count in counts.items())
        self.after(0, lambda: messagebox.showinfo(APP_NAME, f"Exported {summary}.", parent=self))

    def _import_all(self) -> None:
        produced, changed = import_all(
            self._source(), self._required(self.all_export_var, "the export root folder"), self._output(), self._log
        )
        summary = ", ".join(f"{mode}: {count:,}" for mode, count in changed.items())
        self._log(f"Output: {produced}")
        self.after(0, lambda: messagebox.showinfo(APP_NAME, f"Imported {summary}.", parent=self))

    def _log(self, message: str) -> None:
        self.after(0, lambda: (self.log_box.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n"), self.log_box.see("end")))

    def _run_threaded(self, action: Callable[[], None]) -> None:
        def runner() -> None:
            self.after(0, lambda: (self.progress.configure(mode="indeterminate"), self.progress.start(12)))
            try:
                action()
            except Exception as exc:
                self._log(traceback.format_exc())
                error_message = str(exc)
                self.after(0, lambda message=error_message: messagebox.showerror(APP_NAME, message, parent=self))
            finally:
                self.after(0, lambda: (self.progress.stop(), self.progress.configure(mode="determinate", value=0)))
        threading.Thread(target=runner, daemon=True).start()

    def _center(self) -> None:
        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")


def run_self_test(ui: bool = False) -> None:
    raw = b"A\\B\n\x80\xe8\x8c\x01\0"
    usa_map = {
        character: bytes.fromhex(encoded)
        for character, encoded in default_character_map("usa_latin1")["mappings"].items()
    }
    assert text_to_bytes(bytes_to_text(raw, "usa_latin1"), usa_map) == raw
    europe_raw = b"\x1f\x03\x1f\x0e\x1f\x06\x1f\x1b\0"
    europe_map = {
        character: bytes.fromhex(encoded)
        for character, encoded in default_character_map("europe_macroman")["mappings"].items()
    }
    assert text_to_bytes(bytes_to_text(europe_raw, "europe_macroman"), europe_map) == europe_raw
    utf8_raw = "Checking Memory Stick\u2122...\nPlayer ID".encode("utf-8") + b"\0"
    assert text_to_utf8_bytes(utf8_bytes_to_text(utf8_raw)) == utf8_raw
    # Characters the boot screen has a token for survive the round trip.
    boot_text = "\u00c7\u00d6\u00dc\u00c2\u00ce\u00db \u00c9 \u00d1 \u00bf\u00a1 &"
    supported = "".join(ch for ch in boot_text if ch in BOOT_TEXT_TO_ACCENT_BYTES)
    assert boot_bytes_to_text(text_to_boot_bytes(supported)) == supported
    # Uppercase Y-diaeresis uses the compact lookup alias installed on import.
    missing: dict[str, int] = {}
    assert text_to_boot_bytes("\u0178", missing) == b"\x1fZ\0"
    assert missing == {}
    assert boot_bytes_to_text(b"\x1fZ\0") == "\u0178"
    turkish_cells = "KAR[CODE:1F59]I [CODE:1F01][CODE:1F6D]"
    assert text_to_boot_bytes(turkish_cells) == b"KAR\x1fYI \x1f\x01\x1fm\0"
    assert text_to_boot_bytes("\u00c4") == b"&~A\0"
    assert text_to_boot_bytes("\u00cf") == b"&~I\0"
    # 0x1F <code> addresses keepfont cell (code + 0x7F) once the decoder is patched.
    assert [decode_glyph_cell(code) for code in (0x01, 0x0B, 0x16, 0x59, 0x5A, 0x6D)] == [
        0x80, 0x8A, 0x95, 0xD8, 0xD9, 0xEC,
    ]
    codec_block = CodecBlock(
        index=0,
        header_words=[0x455D45CD],
        texts=[CodecText(0, 0x80000000, b"SHORT\0", b"FO")],
        script_header=b"\x01\0\0\0",
        script=struct.pack("<II", 0, 1) + b"\0",
        script_padding=b"",
    )
    rebuilt_codec = build_codec(
        [codec_block],
        {0: {0: b"A TEXT THAT IS LONGER THAN THE ORIGINAL\0"}},
    )
    checked_codec = parse_codec(rebuilt_codec)
    assert checked_codec[0].texts[0].data == b"A TEXT THAT IS LONGER THAN THE ORIGINAL\0"
    assert len(checked_codec[0].texts[0].trailing) <= 3
    # Codec references encode read sectors in bits 20..31, not a file ID.
    # Growing a call must update both fields and leave identical string bytes alone.
    from gcl import patch_gcx_codec_refs, codec_reference_capacities
    prefix = struct.pack('<II', 0x4531E19C, 0xFFFFFFFF)
    old_reference = struct.pack('<I', (21 << 20) | 0x11111)
    inline = b'\x07\x06\x0A' + old_reference + b'\0'
    call = b'\x77\x01\x00\x0A' + old_reference
    main = b'\x8D' + bytes((len(inline+call),)) + inline + call
    script = struct.pack('<II', 0, len(main)) + main
    patched, count = patch_gcx_codec_refs(prefix, script, {0x111110:0}, {0:4}, [512], [50000])
    assert count == 1 and patched[10:10+len(inline)] == inline
    assert codec_reference_capacities(prefix, patched, {512:0}, {0:25}) == {0:25}
    guarded = build_codec([codec_block], {0:{0:b'LONG\0'}}, read_capacities={0:32})
    assert len(guarded) >= 32*2048
    dar = [NamedEntry("one.bin", b"abc"), NamedEntry("two.bin", b"12345")]
    assert parse_dar(build_dar(dar)) == dar
    qar = [NamedEntry("one.txp", b"abc", 1), NamedEntry("two.txp", b"12345", 2)]
    assert parse_qar(build_qar(qar)) == qar
    with tempfile.TemporaryDirectory(prefix="mgspo_test_") as temporary:
        path = Path(temporary) / "test.png"
        pixels = bytes((255, 0, 0, 255, 0, 255, 0, 128))
        write_rgba_png(path, 2, 1, pixels)
        assert read_rgba_png(path) == (2, 1, pixels)
        # Editing an existing font sheet must not append duplicate pixels/CLUTs
        # to the stage cache. Exercise both retail TXP storage modes.
        import zlib
        from txp import import_texture_png, parse_txp
        font_png = Path(temporary) / 'font.png'
        write_rgba_png(font_png, 32, 8, bytes((255,255,255,255)) + bytes((0,0,0,255))*255)
        for compressed in (False, True):
            packed = bytes(128)
            stored = struct.pack('<I', len(zlib.compress(packed))) + zlib.compress(packed) if compressed else packed
            header = struct.pack('<8I', 0,0,1,32,0,48,0,0)
            image = struct.pack('<HHHHII', 32,8,4,2 if compressed else 0,48,176)
            palette = bytes((0,0,0,255,255,255,255,255)) + bytes(56)
            original = header + image + stored + bytes(128-len(stored)) + palette
            edited = import_texture_png(parse_txp(original), {0:font_png})
            assert len(edited) == len(original)
            assert edited[:48] == original[:48] and edited[176:] == palette
            assert parse_txp(edited).textures[0].indices == b'\x01' + bytes(255)
    if ui:
        app = ToolkitApp()
        app.update_idletasks()
        assert app.tabs.index("end") == 4
        app.destroy()
    print("self-test passed")
