from __future__ import annotations

import struct
from pathlib import Path

from common import ToolkitError
from png import read_rgba_png, write_rgba_png


def parse_resident_font(data: bytes) -> tuple[dict, list[dict]]:
    if len(data) < 16:
        raise ToolkitError("Resident font data is too small.")
    table0, bitmap_end = struct.unpack_from("<II", data, 0)
    bitmap_start = table0 & 0x03FFFFFF
    max_y_offset = table0 >> 26
    if not (8 <= bitmap_start < bitmap_end <= len(data)) or (bitmap_start - 8) % 4:
        raise ToolkitError("Resident font offsets are invalid.")
    table_count = (bitmap_start - 8) // 4
    table_values = [struct.unpack_from("<I", data, 8 + index * 4)[0] for index in range(table_count)]
    glyph_count = min(table_count - 1, 0xFF - 0x20)
    heights: list[tuple[int, int]] = []
    for height in range(1, 33):
        matches = 0
        for table_index in range(glyph_count):
            value = table_values[table_index]
            width = ((value >> 20) & 0x3F) + 1
            relative = value & 0x000FFFFF
            next_relative = table_values[table_index + 1] & 0x000FFFFF
            if next_relative - relative == (width * height + 3) // 4:
                matches += 1
        heights.append((matches, height))
    matches, glyph_height = max(heights)
    if matches != glyph_count:
        raise ToolkitError("Resident font glyph height could not be derived from its offset table.")
    glyphs: list[dict] = []
    for table_index in range(glyph_count):
        value = table_values[table_index]
        relative = value & 0x000FFFFF
        width = ((value >> 20) & 0x3F) + 1
        y_offset = (value >> 26) & 0x3F
        next_relative = table_values[table_index + 1] & 0x000FFFFF
        byte_length = next_relative - relative
        absolute = bitmap_start + relative
        if absolute < bitmap_start or absolute + byte_length > bitmap_end:
            continue
        glyphs.append({
            "code": table_index + 0x20,
            "offset": absolute,
            "width": width,
            "height": glyph_height,
            "y_offset": y_offset,
            "byte_length": byte_length,
        })
    return {
        "bitmap_start": bitmap_start,
        "bitmap_end": bitmap_end,
        "max_y_offset": max_y_offset,
        "table_count": table_count,
        "glyph_height": glyph_height,
    }, glyphs


def export_resident_font(data: bytes, path: Path) -> tuple[int, int, int]:
    header, glyphs = parse_resident_font(data)
    max_width = max(int(glyph["width"]) for glyph in glyphs)
    content_height = int(header["glyph_height"]) + int(header["max_y_offset"])
    cell_size = ((max(max_width, content_height) + 3) // 4) * 4
    cell_width = cell_size
    cell_height = cell_size
    columns = 16
    rows = max(1, (len(glyphs) + columns - 1) // columns)
    width = columns * cell_width
    height = rows * cell_height
    rgba = bytearray(b"\0\0\0\xff" * (width * height))
    for glyph_index, glyph in enumerate(glyphs):
        raw = data[int(glyph["offset"]):int(glyph["offset"]) + int(glyph["byte_length"])]
        bit = 0
        for y in range(int(glyph["height"])):
            for x in range(int(glyph["width"])):
                level = (raw[bit // 4] >> (6 - (bit % 4) * 2)) & 3
                bit += 1
                shade = level * 85
                px = (glyph_index % columns) * cell_width + x
                py = (glyph_index // columns) * cell_height + int(glyph["y_offset"]) + y
                offset = (py * width + px) * 4
                rgba[offset:offset + 4] = bytes((shade, shade, shade, 255))
    write_rgba_png(path, width, height, bytes(rgba))
    return width, height, len(glyphs)


def import_resident_font(data: bytes, path: Path) -> bytes:
    header, glyphs = parse_resident_font(data)
    width, height, rgba = read_rgba_png(path)
    max_width = max(int(glyph["width"]) for glyph in glyphs)
    content_height = int(header["glyph_height"]) + int(header["max_y_offset"])
    cell_size = ((max(max_width, content_height) + 3) // 4) * 4
    cell_width = cell_size
    cell_height = cell_size
    expected_width = 16 * cell_width
    expected_height = max(1, (len(glyphs) + 15) // 16) * cell_height
    if (width, height) != (expected_width, expected_height):
        raise ToolkitError(
            f"Resident font canvas changed: expected {expected_width}x{expected_height}, got {width}x{height}."
        )
    output = bytearray(data)
    for glyph_index, glyph in enumerate(glyphs):
        start = int(glyph["offset"])
        raw = bytearray(output[start:start + int(glyph["byte_length"])])
        bit = 0
        for y in range(int(glyph["height"])):
            for x in range(int(glyph["width"])):
                px = (glyph_index % 16) * cell_width + x
                py = (glyph_index // 16) * cell_height + int(glyph["y_offset"]) + y
                offset = (py * width + px) * 4
                red, green, blue, alpha = rgba[offset:offset + 4]
                luminance = ((red * 299 + green * 587 + blue * 114) // 1000) * alpha // 255
                level = min(3, (luminance + 42) // 85)
                shift = 6 - (bit % 4) * 2
                raw[bit // 4] = (raw[bit // 4] & ~(3 << shift)) | (level << shift)
                bit += 1
        output[start:start + len(raw)] = raw
    return bytes(output)
