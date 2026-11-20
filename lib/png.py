from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path

from common import ToolkitError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)


def write_rgba_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        raise ToolkitError("Invalid RGBA image dimensions.")
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)
        rows += pixels[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _chunk(b"IEND", b""))


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    diagonal_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= diagonal_distance:
        return left
    if up_distance <= diagonal_distance:
        return up
    return upper_left


def _unfilter_rows(raw: bytes, cursor: int, count: int, row_size: int, filter_stride: int) -> tuple[list[bytes], int]:
    rows: list[bytes] = []
    previous = bytes(row_size)
    for _y in range(count):
        filter_type = raw[cursor]
        cursor += 1
        scanline = bytearray(raw[cursor:cursor + row_size])
        cursor += row_size
        for index in range(row_size):
            left = scanline[index - filter_stride] if index >= filter_stride else 0
            up = previous[index]
            upper_left = previous[index - filter_stride] if index >= filter_stride else 0
            if filter_type == 1:
                scanline[index] = (scanline[index] + left) & 0xFF
            elif filter_type == 2:
                scanline[index] = (scanline[index] + up) & 0xFF
            elif filter_type == 3:
                scanline[index] = (scanline[index] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                scanline[index] = (scanline[index] + _paeth(left, up, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ToolkitError(f"Unsupported PNG row filter: {filter_type}")
        previous = bytes(scanline)
        rows.append(previous)
    return rows, cursor


_ADAM7_PASSES = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _samples(row: bytes, bit_depth: int, count: int) -> list[int]:
    if bit_depth == 8:
        return list(row[:count])
    if bit_depth == 16:
        return [row[index * 2] for index in range(count)]
    mask = (1 << bit_depth) - 1
    values: list[int] = []
    bit_position = 0
    for _index in range(count):
        byte_index = bit_position // 8
        shift = 8 - bit_depth - bit_position % 8
        values.append((row[byte_index] >> shift) & mask)
        bit_position += bit_depth
    return values


def read_rgba_png(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ToolkitError(f"Not a PNG file: {path}")
    position = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    palette: list[tuple[int, int, int]] = []
    transparency = b""
    compressed = bytearray()
    while position + 12 <= len(data):
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        payload_start = position + 8
        payload_end = payload_start + length
        if payload_end + 4 > len(data):
            raise ToolkitError(f"Truncated PNG chunk in {path}")
        payload = data[payload_start:payload_end]
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise ToolkitError("Interlaced or non-standard PNG files are not supported.")
        elif kind == b"PLTE":
            if len(payload) % 3:
                raise ToolkitError("Invalid PNG palette.")
            palette = [tuple(payload[index:index + 3]) for index in range(0, len(payload), 3)]
        elif kind == b"tRNS":
            transparency = payload
        elif kind == b"IDAT":
            compressed += payload
        elif kind == b"IEND":
            break
        position = payload_end + 4
    if width is None or height is None or bit_depth is None or color_type is None:
        raise ToolkitError(f"PNG header is missing: {path}")
    channel_counts = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channel_counts.get(color_type)
    if channels is None or bit_depth not in (1, 2, 4, 8, 16):
        raise ToolkitError(f"Unsupported PNG color format: type {color_type}, {bit_depth}-bit")
    if color_type in (2, 4, 6) and bit_depth not in (8, 16):
        raise ToolkitError(f"Unsupported PNG color depth for type {color_type}: {bit_depth}")
    bits_per_pixel = channels * bit_depth
    row_size = (width * bits_per_pixel + 7) // 8
    filter_stride = max(1, (bits_per_pixel + 7) // 8)
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ToolkitError(f"Invalid PNG compression stream: {exc}") from exc
    decoded_rows: list[bytes] = []
    if interlace == 0:
        if len(raw) != height * (row_size + 1):
            raise ToolkitError("PNG pixel stream has an unexpected size.")
        decoded_rows, _ = _unfilter_rows(raw, 0, height, row_size, filter_stride)
    elif interlace == 1:
        if bit_depth not in (8, 16):
            raise ToolkitError(f"Unsupported interlaced PNG color depth: {bit_depth}-bit")
        sample_size = channels * (bit_depth // 8)
        grid = [bytearray(width * sample_size) for _y in range(height)]
        cursor = 0
        for x0, y0, dx, dy in _ADAM7_PASSES:
            pass_width = (width - x0 + dx - 1) // dx if x0 < width else 0
            pass_height = (height - y0 + dy - 1) // dy if y0 < height else 0
            if pass_width <= 0 or pass_height <= 0:
                continue
            pass_row_size = pass_width * sample_size
            pass_rows, cursor = _unfilter_rows(raw, cursor, pass_height, pass_row_size, sample_size)
            for py in range(pass_height):
                for px in range(pass_width):
                    sample = pass_rows[py][px * sample_size:(px + 1) * sample_size]
                    offset = (x0 + px * dx) * sample_size
                    grid[y0 + py * dy][offset:offset + sample_size] = sample
        if cursor != len(raw):
            raise ToolkitError("Interlaced PNG pixel stream has an unexpected size.")
        decoded_rows = [bytes(row) for row in grid]
    else:
        raise ToolkitError("Interlaced or non-standard PNG files are not supported.")
    rgba = bytearray()
    maximum = (1 << bit_depth) - 1
    for row in decoded_rows:
        values = _samples(row, bit_depth, width * channels)
        for x in range(width):
            start = x * channels
            if color_type == 0:
                gray = values[start] * 255 // maximum
                rgba += bytes((gray, gray, gray, 255))
            elif color_type == 2:
                rgba += bytes((values[start], values[start + 1], values[start + 2], 255))
            elif color_type == 3:
                palette_index = values[start]
                if palette_index >= len(palette):
                    raise ToolkitError("PNG palette index is out of range.")
                red, green, blue = palette[palette_index]
                alpha = transparency[palette_index] if palette_index < len(transparency) else 255
                rgba += bytes((red, green, blue, alpha))
            elif color_type == 4:
                gray = values[start]
                rgba += bytes((gray, gray, gray, values[start + 1]))
            else:
                rgba += bytes(values[start:start + 4])
    return width, height, bytes(rgba)
