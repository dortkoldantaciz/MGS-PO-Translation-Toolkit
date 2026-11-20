from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


SHORT_APP_NAME = "MGS PO Tranlation Tool"
APP_NAME = "MGS Portable Ops Translation Toolkit"
LONG_APP_NAME = "Metal Gear Solid Portable Ops Translation Toolkit"
APP_VERSION = "0.9.3"

WORK_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)

TEXT_FILE_NAME = "mgs_portable_ops_text.txt"
TEXT_MANIFEST_NAME = "mgs_portable_ops_text.manifest.json"
TEXT_IMPORT_REPORT_NAME = "mgs_portable_ops_text_import_report.csv"
TEXT_LINE_INDEX_NAME = "mgs_portable_ops_text_lines.csv"
ASSET_MANIFEST_NAME = "manifest.json"
EMPTY_TEXT_TOKEN = "<EMPTY>"
LINE_FEED_TEXT_TOKEN = "|"
CODEC_NEWLINE_TEXT_TOKEN = "/n"
CRLF_TEXT_TOKEN = "\\n"
PAIR_TOKENS = {
    0x807C: CODEC_NEWLINE_TEXT_TOKEN,
    0xA07B: "[B]",
    0xC07D: "[/B]",
    0xC027: "[APOSTROPHE]",
}
TOKEN_PAIRS = {value: key for key, value in PAIR_TOKENS.items()}

# European Portable Ops scripts store MacRoman glyph codes after 0x1F.
MACROMAN_LATIN_CHARACTERS = {
    0x01: "Ä", 0x02: "Å", 0x03: "Ç", 0x04: "É", 0x05: "Ñ", 0x06: "Ö", 0x07: "Ü",
    0x08: "á", 0x09: "à", 0x0A: "â", 0x0B: "ä", 0x0C: "ã", 0x0D: "å", 0x0E: "ç",
    0x0F: "é", 0x10: "è", 0x11: "ê", 0x12: "ë", 0x13: "í", 0x14: "ì", 0x15: "î",
    0x16: "ï", 0x17: "ñ", 0x18: "ó", 0x19: "ò", 0x1A: "ô", 0x1B: "ö", 0x1C: "õ",
    0x1D: "ú", 0x1E: "ù", 0x1F: "û", 0x20: "ü", 0x21: "†", 0x22: "°", 0x23: "¢",
    0x24: "£", 0x25: "§", 0x26: "•", 0x27: "¶", 0x28: "ß", 0x29: "®", 0x2A: "©",
    0x2B: "™", 0x2C: "´", 0x2D: "¨", 0x2E: "≠", 0x2F: "Æ", 0x30: "Ø", 0x31: "∞",
    0x32: "±", 0x33: "≤", 0x34: "≥", 0x35: "¥", 0x36: "µ", 0x37: "∂", 0x38: "∑",
    0x39: "∏", 0x3A: "π", 0x3B: "∫", 0x3C: "ª", 0x3D: "º", 0x3E: "Ω", 0x3F: "æ",
    0x40: "ø", 0x41: "¿", 0x42: "¡", 0x43: "¬", 0x44: "√", 0x45: "ƒ", 0x46: "≈",
    0x47: "∆", 0x48: "«", 0x49: "»", 0x4A: "…", 0x4B: "n", 0x4C: "À", 0x4D: "Ã",
    0x4E: "Õ", 0x4F: "Œ", 0x50: "œ", 0x51: "–", 0x52: "—", 0x53: "“", 0x54: "”",
    0x55: "‘", 0x56: "’", 0x57: "÷", 0x58: "◊", 0x59: "ÿ", 0x5A: "Ÿ", 0x5B: "⁄",
    0x5C: "€", 0x5D: "‹", 0x5E: "›", 0x5F: "ﬁ", 0x60: "ﬂ", 0x61: "‡", 0x62: "·",
    0x63: "‚", 0x64: "„", 0x65: "‰", 0x66: "Â", 0x67: "Ê", 0x68: "Á", 0x69: "Ë",
    0x6A: "È", 0x6B: "Í", 0x6C: "Î", 0x6D: "Ï", 0x6E: "Ì", 0x6F: "Ó", 0x70: "Ô",
    0x72: "Ò", 0x73: "Ú", 0x74: "Û", 0x75: "Ù", 0x76: "ı", 0x77: "ˆ", 0x78: "˜",
    0x79: "¯", 0x7A: "˘", 0x7B: "˙", 0x7C: "˚", 0x7D: "¸", 0x7E: "˝",
}
USA_LATIN1_CHARACTERS = {
    codepoint - 0x7F: chr(codepoint)
    for codepoint in range(0xA0, 0xFF)
}
TEXT_CHARACTER_TABLES = {
    "usa_latin1": USA_LATIN1_CHARACTERS,
    "europe_macroman": MACROMAN_LATIN_CHARACTERS,
}

class ToolkitError(RuntimeError):
    pass


def align(value: int, boundary: int) -> int:
    return ((value + boundary - 1) // boundary) * boundary


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolkitError(f"Could not read manifest: {path}\n{exc}") from exc


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def character_table(profile: str) -> dict[int, str]:
    try:
        return TEXT_CHARACTER_TABLES[profile]
    except KeyError as exc:
        raise ToolkitError(f"Unsupported Portable Ops text encoding profile: {profile}") from exc


def default_character_map(profile: str = "usa_latin1") -> dict:
    table = character_table(profile)
    mappings = {}
    for code, character in table.items():
        if character.isascii() or not character.isprintable():
            continue
        if profile == "usa_latin1":
            mappings[character] = bytes((0x80, 0x7F + code)).hex().upper()
        else:
            mappings[character] = bytes((0x1F, code)).hex().upper()
    slots: list[dict] = []
    for second_byte in range(0x01, 0x7F):
        keepfont_code = second_byte + 0x7F
        code = f"1F{second_byte:02X}"
        glyph_index = keepfont_code - 0x20
        slot = {
            "code": code,
            "keepfont_code": f"{keepfont_code:02X}",
            "atlas_column": glyph_index % 16,
            "atlas_row": glyph_index // 16,
        }
        character = table.get(second_byte)
        if character is not None:
            slot["character"] = character
        slots.append(slot)
    return {
        "format": "mgs-portable-ops-character-map",
        "format_version": 1,
        "text_profile": profile,
        "font_assets": ["keepfont.row", "keepfont_mini.row"],
        "atlas_columns": 16,
        "cell_width": 24,
        "mappings": mappings,
        "available_font_slots": slots,
    }


def safe_file_part(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    out: list[str] = []
    previous_was_separator = False
    for char in value.replace("\\", "/"):
        if char in allowed:
            out.append(char)
            previous_was_separator = False
        elif not previous_was_separator:
            out.append("_")
            previous_was_separator = True
    result = "".join(out).strip("_")
    return result or "file"


def detect_game_region(usrdir: Path) -> str:
    """'usa' for ULUS-10202 (272 codec blocks), 'europe' for the EU release (294), else 'unknown'."""
    from codec import parse_codec

    codec_path = usrdir / "codec.dat"
    if not codec_path.is_file():
        return "unknown"
    try:
        count = len(parse_codec(codec_path.read_bytes()))
    except Exception:
        return "unknown"
    if count == 272:
        return "usa"
    if count == 294:
        return "europe"
    return "unknown"


def resolve_usrdir(source: Path) -> tuple[Path, Path]:
    source = source.resolve()
    candidates = (
        source,
        source / "PSP_GAME" / "USRDIR",
        source / "USRDIR",
    )
    for candidate in candidates:
        if (candidate / "codec.dat").is_file() and (candidate / "stage").is_dir():
            if candidate.name.upper() == "USRDIR" and candidate.parent.name.upper() == "PSP_GAME":
                return candidate, candidate.parent.parent
            return candidate, source
    raise ToolkitError(
        "Select the extracted game folder that contains PSP_GAME/USRDIR/codec.dat, "
        "or select the USRDIR folder itself."
    )


def prepare_rebuild_root(source: Path, destination: Path) -> tuple[Path, Path]:
    source_usrdir, game_root = resolve_usrdir(source)
    destination = destination.resolve()
    if destination == game_root or game_root in destination.parents:
        raise ToolkitError("The rebuild folder must be outside the source game folder.")
    if destination.exists():
        if any(destination.iterdir()):
            raise ToolkitError("The rebuild folder must be empty or not exist yet.")
    else:
        destination.mkdir(parents=True)
    rebuilt_root = destination / game_root.name
    shutil.copytree(game_root, rebuilt_root)
    relative_usrdir = source_usrdir.relative_to(game_root)
    return rebuilt_root, rebuilt_root / relative_usrdir


def bytes_to_text(raw: bytes, profile: str = "usa_latin1") -> str:
    table = character_table(profile)
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    if not raw:
        return EMPTY_TEXT_TOKEN
    out: list[str] = []
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 0x0D and index + 1 < len(raw) and raw[index + 1] == 0x0A:
            out.append(CRLF_TEXT_TOKEN)
            index += 1
        elif value == 0x5C:
            out.append("\\\\")
        elif value == 0x7C:
            out.append("\\|")
        elif value == 0x0A:
            out.append(LINE_FEED_TEXT_TOKEN)
        elif value == 0x0D:
            out.append("\\r")
        elif value == 0x09:
            out.append("\\t")
        elif value == 0x1F and index + 1 < len(raw):
            second_byte = raw[index + 1]
            out.append(table.get(second_byte, f"[CODE:1F{second_byte:02X}]"))
            index += 1
        elif value == 0x80 and profile == "usa_latin1" and index + 1 < len(raw):
            glyph = raw[index + 1]
            code = glyph - 0x7F
            if code in table:
                out.append(table[code])
            elif 0x20 <= glyph <= 0x7E:
                out.append(chr(glyph))
            else:
                out.append(f"[CODE:80{glyph:02X}]")
            index += 1
        elif 0x20 <= value <= 0x7E:
            out.append(chr(value))
        elif value >= 0x80 and index + 1 < len(raw):
            pair = (value << 8) | raw[index + 1]
            out.append(PAIR_TOKENS.get(pair, f"[CODE:{pair:04X}]"))
            index += 1
        else:
            out.append(f"[BYTE:{value:02X}]")
        index += 1
    return "".join(out)


def text_to_bytes(text: str, character_map: dict[str, bytes] | None = None) -> bytes:
    if text == EMPTY_TEXT_TOKEN:
        return b"\0"
    out = bytearray()
    index = 0
    while index < len(text):
        char = text[index]
        if text.startswith("[CODE:", index):
            end = text.find("]", index + 6)
            digits = text[index + 6:end] if end >= 0 else ""
            if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ToolkitError("An invalid [CODE:NNNN] token was found.")
            value = int(digits, 16)
            out.extend((value >> 8, value & 0xFF))
            index = end + 1
            continue
        if text.startswith("[BYTE:", index):
            end = text.find("]", index + 6)
            digits = text[index + 6:end] if end >= 0 else ""
            if len(digits) != 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ToolkitError("An invalid [BYTE:NN] token was found.")
            out.append(int(digits, 16))
            index = end + 1
            continue
        matched_token = next((token for token in TOKEN_PAIRS if text.startswith(token, index)), None)
        if matched_token is not None:
            pair = TOKEN_PAIRS[matched_token]
            out.extend((pair >> 8, pair & 0xFF))
            index += len(matched_token)
            continue
        if text.startswith(CRLF_TEXT_TOKEN, index):
            out += b"\r\n"
            index += len(CRLF_TEXT_TOKEN)
            continue
        if char == LINE_FEED_TEXT_TOKEN:
            out.append(0x0A)
            index += 1
            continue
        if char != "\\":
            mapped = character_map.get(char) if character_map else None
            if mapped is not None:
                out += mapped
                index += 1
                continue
            code = ord(char)
            if 0x20 <= code <= 0x7E:
                out.append(code)
            else:
                raise ToolkitError(
                    f"Unsupported character {char!r}. Assign it to an existing game code and edit the matching font glyph."
                )
            index += 1
            continue
        if index + 1 >= len(text):
            raise ToolkitError("A text line ends with an incomplete escape character.")
        marker = text[index + 1]
        if marker == "\\":
            out.append(0x5C)
            index += 2
        elif marker == "|":
            out.append(0x7C)
            index += 2
        elif marker == "r":
            out.append(0x0D)
            index += 2
        elif marker == "t":
            out.append(0x09)
            index += 2
        elif marker == "x":
            if index + 4 > len(text):
                raise ToolkitError("An incomplete \\xNN byte escape was found.")
            digit_count = 2
            first_pair = text[index + 2:index + 4]
            four_digits = text[index + 2:index + 6]
            if (
                len(four_digits) == 4
                and all(char in "0123456789abcdefABCDEF" for char in four_digits)
                and int(first_pair, 16) >= 0x80
            ):
                digit_count = 4
            digits = text[index + 2:index + 2 + digit_count]
            try:
                value = int(digits, 16)
            except ValueError as exc:
                raise ToolkitError(f"Invalid byte escape: \\x{digits}") from exc
            if digit_count == 2:
                out.append(value)
            else:
                out.extend((value >> 8, value & 0xFF))
            index += 2 + digit_count
        else:
            raise ToolkitError(f"Unknown text escape: \\{marker}")
    return bytes(out) + b"\0"


def gcl_bytes_to_text(raw: bytes, profile: str = "usa_latin1") -> str:
    table = character_table(profile)
    if not raw:
        return EMPTY_TEXT_TOKEN
    output: list[str] = []
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 0x1F and index + 1 < len(raw):
            second_byte = raw[index + 1]
            output.append(table.get(second_byte, f"[CODE:1F{second_byte:02X}]"))
            index += 2
            continue
        if 0xA1 <= value <= 0xFE and index + 1 < len(raw) and 0xA1 <= raw[index + 1] <= 0xFE:
            output.append(raw[index:index + 2].decode("euc_jp"))
            index += 2
            continue
        if value >= 0x80 and index + 1 < len(raw):
            pair = (value << 8) | raw[index + 1]
            output.append(PAIR_TOKENS.get(pair, f"[CODE:{pair:04X}]"))
            index += 2
            continue
        output.append(bytes_to_text(bytes((value,)), profile))
        index += 1
    return "".join(output)


def text_to_gcl_bytes(text: str, character_map: dict[str, bytes] | None = None) -> bytes:
    if text == EMPTY_TEXT_TOKEN:
        return b""
    output = bytearray()
    index = 0
    while index < len(text):
        if text.startswith("[CODE:", index):
            end = text.find("]", index + 6)
            digits = text[index + 6:end] if end >= 0 else ""
            if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ToolkitError("An invalid [CODE:NNNN] token was found.")
            output += int(digits, 16).to_bytes(2, "big")
            index = end + 1
            continue
        if text.startswith("[BYTE:", index):
            end = text.find("]", index + 6)
            digits = text[index + 6:end] if end >= 0 else ""
            if len(digits) != 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ToolkitError("An invalid [BYTE:NN] token was found.")
            output.append(int(digits, 16))
            index = end + 1
            continue
        token = next((item for item in TOKEN_PAIRS if text.startswith(item, index)), None)
        if token is not None:
            pair = TOKEN_PAIRS[token]
            output.extend((pair >> 8, pair & 0xFF))
            index += len(token)
            continue
        if text.startswith(CRLF_TEXT_TOKEN, index):
            output += b"\r\n"
            index += len(CRLF_TEXT_TOKEN)
            continue
        char = text[index]
        if char == LINE_FEED_TEXT_TOKEN:
            output.append(0x0A)
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(text):
                raise ToolkitError("A GCL text line ends with an incomplete escape character.")
            marker = text[index + 1]
            if marker == "\\":
                output.append(0x5C)
                index += 2
                continue
            if marker == "|":
                output.append(0x7C)
                index += 2
                continue
            if marker == "r":
                output.append(0x0D)
                index += 2
                continue
            if marker == "t":
                output.append(0x09)
                index += 2
                continue
            if marker == "x":
                four = text[index + 2:index + 6]
                two = text[index + 2:index + 4]
                digits = four if len(four) == 4 and all(c in "0123456789abcdefABCDEF" for c in four) else two
                if len(digits) not in (2, 4) or any(c not in "0123456789abcdefABCDEF" for c in digits):
                    raise ToolkitError("An invalid GCL byte token was found.")
                value = int(digits, 16)
                output += value.to_bytes(len(digits) // 2, "big")
                index += 2 + len(digits)
                continue
            raise ToolkitError(f"Unknown GCL text escape: \\{marker}")
        try:
            encoded = char.encode("euc_jp")
        except UnicodeEncodeError:
            encoded = b""
        if len(encoded) == 2 and encoded[0] >= 0xA1:
            output += encoded
            index += 1
            continue
        encoded_line = text_to_bytes(char, character_map)
        output += encoded_line[:-1]
        index += 1
    return bytes(output)


def utf8_bytes_to_text(raw: bytes) -> str:
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    if not raw:
        return EMPTY_TEXT_TOKEN
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolkitError("A UTF-8 text resource contains invalid data.") from exc
    output: list[str] = []
    for char in value:
        if char == "\\":
            output.append("\\\\")
        elif char == "|":
            output.append("\\|")
        elif char == "\n":
            output.append(LINE_FEED_TEXT_TOKEN)
        elif char == "\r":
            output.append("\\r")
        elif char == "\t":
            output.append("\\t")
        else:
            output.append(char)
    return "".join(output)


def text_to_utf8_bytes(text: str) -> bytes:
    if text == EMPTY_TEXT_TOKEN:
        return b"\0"
    output: list[str] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char == LINE_FEED_TEXT_TOKEN:
            output.append("\n")
            position += 1
            continue
        if char != "\\":
            output.append(char)
            position += 1
            continue
        if position + 1 >= len(text):
            raise ToolkitError("A UTF-8 text line ends with an incomplete escape character.")
        marker = text[position + 1]
        replacements = {"\\": "\\", "|": "|", "r": "\r", "t": "\t"}
        if marker not in replacements:
            raise ToolkitError(f"Unknown UTF-8 text escape: \\{marker}")
        output.append(replacements[marker])
        position += 2
    return "".join(output).encode("utf-8") + b"\0"


def read_text_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ToolkitError(f"Could not read text file: {path}\n{exc}") from exc
    return text.splitlines()


def write_text_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
