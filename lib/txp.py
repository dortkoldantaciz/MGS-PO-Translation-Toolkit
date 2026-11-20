from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from common import ToolkitError, align
from png import read_rgba_png, write_rgba_png


@dataclass
class TxpTexture:
    index: int
    width_word: int
    height_word: int
    bpp_word: int
    compression_word: int
    data_offset: int
    palette_offset: int
    indices: bytes
    palette: list[tuple[int, int, int, int]]

    @property
    def width(self) -> int:
        return self.width_word & 0x0FFF

    @property
    def height(self) -> int:
        return self.height_word & 0x0FFF

    @property
    def bpp(self) -> int:
        return self.bpp_word & 0x000F

    @property
    def palette_size(self) -> int:
        return 16 if self.bpp == 4 else 256


@dataclass
class TxpFile:
    data: bytes
    header_words: tuple[int, int, int, int, int, int, int, int]
    textures: list[TxpTexture]

    @property
    def texture_table_offset(self) -> int:
        return self.header_words[3]


def _unswizzle(source: bytes, width: int, height: int, bpp: int) -> bytes:
    pixels = bytearray(width * height)
    output = 0
    for y in range(height):
        y_base = y * 16
        if bpp == 4:
            y_block = (y // 8) * (width * 4 - 128)
            for x in range(width // 2):
                x_local = (x // 16) * 16
                x_block = (x // 16) * 128
                value = source[x - x_local + x_block + y_base + y_block]
                pixels[output] = value & 0x0F
                pixels[output + 1] = value >> 4
                output += 2
        else:
            y_block = (y // 8) * (width * 8 - 128)
            for x in range(width):
                x_local = (x // 16) * 16
                x_block = (x // 16) * 128
                pixels[output] = source[x - x_local + x_block + y_base + y_block]
                output += 1
    return bytes(pixels)


def _swizzle(indices: bytes, width: int, height: int, bpp: int) -> bytes:
    packed_size = width * height // (2 if bpp == 4 else 1)
    packed = bytearray(packed_size)
    source_index = 0
    for y in range(height):
        y_base = y * 16
        if bpp == 4:
            y_block = (y // 8) * (width * 4 - 128)
            for x in range(width // 2):
                x_local = (x // 16) * 16
                x_block = (x // 16) * 128
                destination = x - x_local + x_block + y_base + y_block
                packed[destination] = indices[source_index] | (indices[source_index + 1] << 4)
                source_index += 2
        else:
            y_block = (y // 8) * (width * 8 - 128)
            for x in range(width):
                x_local = (x // 16) * 16
                x_block = (x // 16) * 128
                destination = x - x_local + x_block + y_base + y_block
                packed[destination] = indices[source_index]
                source_index += 1
    return bytes(packed)


def parse_txp(data: bytes) -> TxpFile:
    if len(data) < 32:
        raise ToolkitError("TXP file is too small.")
    header = struct.unpack_from("<8I", data, 0)
    if header[1] >= 256:
        raise ToolkitError("This TXP uses the Peace Walker layout, not Portable Ops.")
    texture_count, texture_offset = header[2], header[3]
    if texture_count > 100_000 or texture_offset + texture_count * 16 > len(data):
        raise ToolkitError("TXP texture table is invalid.")
    textures: list[TxpTexture] = []
    for index in range(texture_count):
        values = struct.unpack_from("<HHHHII", data, texture_offset + index * 16)
        width_word, height_word, bpp_word, compression_word, data_offset, palette_offset = values
        width, height, bpp = width_word & 0x0FFF, height_word & 0x0FFF, bpp_word & 0x000F
        if width < 16 or height < 8 or bpp not in (4, 5):
            raise ToolkitError(f"Unsupported TXP texture {index}: {width}x{height}, bpp mode {bpp}")
        packed_size = width * height // (2 if bpp == 4 else 1)
        compression = compression_word & 0x000F
        if compression:
            if data_offset + 4 > len(data):
                raise ToolkitError(f"TXP texture {index} compression header is truncated.")
            compressed_size = struct.unpack_from("<I", data, data_offset)[0]
            try:
                packed = zlib.decompress(data[data_offset + 4:data_offset + 4 + compressed_size])
            except zlib.error as exc:
                raise ToolkitError(f"TXP texture {index} has invalid compression: {exc}") from exc
        else:
            packed = bytes(data[data_offset:data_offset + packed_size])
        if len(packed) != packed_size:
            raise ToolkitError(f"TXP texture {index} pixel data is truncated.")
        palette_count = 16 if bpp == 4 else 256
        palette_end = palette_offset + palette_count * 4
        if palette_end > len(data):
            raise ToolkitError(f"TXP texture {index} palette is truncated.")
        palette = [tuple(data[position:position + 4]) for position in range(palette_offset, palette_end, 4)]
        textures.append(
            TxpTexture(
                index, width_word, height_word, bpp_word, compression_word,
                data_offset, palette_offset, _unswizzle(packed, width, height, bpp), palette,
            )
        )
    return TxpFile(data=data, header_words=header, textures=textures)


def texture_rgba(texture: TxpTexture) -> bytes:
    out = bytearray()
    for palette_index in texture.indices:
        out += bytes(texture.palette[palette_index])
    return bytes(out)


def export_texture_png(txp: TxpFile, texture_index: int, path: Path) -> None:
    texture = txp.textures[texture_index]
    write_rgba_png(path, texture.width, texture.height, texture_rgba(texture))


def _map_to_palette(pixels: bytes, palette: list[tuple[int, int, int, int]]) -> bytes:
    pixel_values = [tuple(pixels[index:index + 4]) for index in range(0, len(pixels), 4)]
    exact: dict[tuple[int, int, int, int], int] = {}
    for index, color in enumerate(palette):
        exact.setdefault(color, index)
    lookup: dict[tuple[int, int, int, int], int] = {}
    indices = bytearray()
    for color in pixel_values:
        palette_index = exact.get(color)
        if palette_index is None:
            palette_index = lookup.get(color)
        if palette_index is None:
            palette_index = min(
                range(len(palette)),
                key=lambda candidate: sum(
                    (color[channel] - palette[candidate][channel]) ** 2 for channel in range(4)
                ),
            )
            lookup[color] = palette_index
        indices.append(palette_index)
    return bytes(indices)


def import_texture_png(txp: TxpFile, replacements: dict[int, Path]) -> bytes:
    if not replacements:
        return txp.data
    out = bytearray(txp.data)
    for texture_index, path in sorted(replacements.items()):
        if texture_index < 0 or texture_index >= len(txp.textures):
            raise ToolkitError(f"TXP texture index is invalid: {texture_index}")
        texture = txp.textures[texture_index]
        width, height, rgba = read_rgba_png(path)
        if (width, height) != (texture.width, texture.height):
            raise ToolkitError(
                f"Texture canvas changed for {path.name}: expected {texture.width}x{texture.height}, got {width}x{height}."
            )
        indices = _map_to_palette(rgba, texture.palette)
        packed = _swizzle(indices, texture.width, texture.height, texture.bpp)
        compression = texture.compression_word & 0x000F
        if compression:
            compressed = zlib.compress(packed, 9)
            payload = struct.pack("<I", len(compressed)) + compressed
        else:
            payload = packed
        # The game loads the complete TXP into its stage cache. Appending a
        # second copy of every edited font needlessly exhausted that cache.
        # Pixel data occupies [data_offset, palette_offset); keep both pointers,
        # the CLUT, image/sub-image tables and the file size when it fits.
        capacity = texture.palette_offset - texture.data_offset
        shared = any(other.index != texture_index and other.data_offset == texture.data_offset
                     for other in txp.textures)
        crossing = any(texture.data_offset < boundary < texture.palette_offset
                       for other in txp.textures if other.index != texture_index
                       for boundary in (other.data_offset, other.palette_offset))
        if not shared and not crossing and 0 < len(payload) <= capacity:
            out[texture.data_offset:texture.palette_offset] = payload + bytes(capacity-len(payload))
            continue
        out += b"\0" * (align(len(out), 16) - len(out))
        data_offset = len(out)
        out += payload
        out += b"\0" * (align(len(out), 16) - len(out))
        palette_offset = len(out)
        for color in texture.palette:
            out += bytes(color)
        header_offset = txp.texture_table_offset + texture_index * 16
        struct.pack_into(
            "<HHHHII", out, header_offset,
            texture.width_word, texture.height_word, texture.bpp_word, texture.compression_word,
            data_offset, palette_offset,
        )
    return bytes(out)
