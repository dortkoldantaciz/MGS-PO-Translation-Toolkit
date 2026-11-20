from __future__ import annotations

import struct
from dataclasses import dataclass

from common import ToolkitError
from gcl import _GclCodec


@dataclass
class CodecText:
    index: int
    table_value: int
    data: bytes
    trailing: bytes


@dataclass
class CodecBlock:
    index: int
    header_words: list[int]
    texts: list[CodecText]
    script_header: bytes
    script: bytes
    script_padding: bytes
    source_offset: int = 0
    source_size: int = 0


def _split_script_padding(
    header_words: list[int], raw_script: bytes, block_index: int, allow_read_guard: bool = False
) -> tuple[bytes, bytes]:
    if len(raw_script) < 8:
        raise ToolkitError(f"codec.dat block {block_index} script is truncated.")
    ends: list[int] = []
    for value in header_words[1:]:
        start = 4 + (value & 0x00FFFFFF)
        if start >= len(raw_script):
            raise ToolkitError(
                f"codec.dat block {block_index} procedure points outside its script."
            )
        _rebuilt, end = _GclCodec().token(raw_script, start, len(raw_script))
        ends.append(end)
    main_offset = struct.unpack_from("<I", raw_script, 0)[0]
    main_size_position = 4 + main_offset
    main_start = main_size_position + 4
    if main_start >= len(raw_script):
        raise ToolkitError(
            f"codec.dat block {block_index} main procedure points outside its script."
        )
    _rebuilt, main_end = _GclCodec().token(raw_script, main_start, len(raw_script))
    ends.append(main_end)
    used_end = max(ends)
    padding = bytes(raw_script[used_end:])
    # Stable reference layouts can contain zero-filled holes after moved blocks.
    # Bytecode parsing above establishes the logical end independently of them.
    if any(padding):
        raise ToolkitError(
            f"codec.dat block {block_index} has invalid script alignment padding."
        )
    return bytes(raw_script[:used_end]), padding


def _candidate_starts(data: bytes) -> list[int]:
    if len(data) < 4:
        raise ToolkitError("codec.dat is too small.")
    delimiter = struct.unpack_from("<I", data, 0)[0]
    starts: list[int] = []
    for offset in range(0, len(data) - 3, 4):
        if struct.unpack_from("<I", data, offset)[0] == delimiter:
            starts.append(offset)
    if not starts or starts[0] != 0:
        raise ToolkitError("codec.dat block marker was not found.")
    starts.append(len(data))
    return starts


def parse_codec(data: bytes) -> list[CodecBlock]:
    starts = _candidate_starts(data)
    blocks: list[CodecBlock] = []
    for block_index, start in enumerate(starts[:-1]):
        end = starts[block_index + 1]
        position = start
        header_words: list[int] = []
        while True:
            if position + 4 > end:
                raise ToolkitError(f"codec.dat block {block_index} has no header terminator.")
            value = struct.unpack_from("<i", data, position)[0]
            position += 4
            if value == -1:
                break
            header_words.append(value)
        base = position
        if base + 20 > end:
            raise ToolkitError(f"codec.dat block {block_index} header is truncated.")
        script_offset, table_offset, text_offset, script_header_offset, key = struct.unpack_from(
            "<IIIII", data, base
        )
        if key != 0:
            raise ToolkitError(f"Encrypted codec.dat block {block_index} is not supported.")
        if not (20 <= table_offset <= text_offset <= script_header_offset <= script_offset <= end - base):
            raise ToolkitError(f"codec.dat block {block_index} offsets are invalid.")
        if (text_offset - table_offset) % 4:
            raise ToolkitError(f"codec.dat block {block_index} text table is misaligned.")
        count = (text_offset - table_offset) // 4
        values = [struct.unpack_from("<I", data, base + table_offset + index * 4)[0] for index in range(count)]
        offsets = [value & 0x7FFFFFFF for value in values]
        if offsets != sorted(offsets):
            raise ToolkitError(f"codec.dat block {block_index} text offsets are not ordered.")
        texts: list[CodecText] = []
        pool_size = script_header_offset - text_offset
        for text_index, relative_offset in enumerate(offsets):
            next_offset = offsets[text_index + 1] if text_index + 1 < count else pool_size
            if relative_offset > next_offset or next_offset > pool_size:
                raise ToolkitError(f"codec.dat block {block_index} text {text_index} is invalid.")
            raw = bytes(data[base + text_offset + relative_offset:base + text_offset + next_offset])
            nul = raw.find(b"\0")
            if nul < 0:
                text_data = raw
                trailing = b""
            else:
                text_data = raw[:nul + 1]
                trailing = raw[nul + 1:]
            texts.append(CodecText(text_index, values[text_index], text_data, trailing))
        if texts and texts[-1].trailing != b"FON"[:len(texts[-1].trailing)]:
            raise ToolkitError(
                f"codec.dat block {block_index} has invalid text alignment padding."
            )
        raw_script = bytes(data[base + script_offset:end])
        script, script_padding = _split_script_padding(
            header_words, raw_script, block_index, block_index == len(starts) - 2
        )
        blocks.append(
            CodecBlock(
                index=block_index,
                header_words=header_words,
                texts=texts,
                script_header=bytes(data[base + script_header_offset:base + script_offset]),
                script=script,
                script_padding=script_padding,
                source_offset=start,
                source_size=end-start,
            )
        )
    return blocks


def english_text_indices(block: CodecBlock, text_profile: str) -> range:
    if text_profile != "europe_macroman" or not block.texts:
        return range(len(block.texts))
    french_offsets = [
        value & 0x0FFFFFFF
        for value in block.header_words
        if ((value & 0xFFFFFFFF) >> 28) == 2
    ]
    if not french_offsets:
        return range(0)
    boundary = min(french_offsets)
    if boundary + 4 > len(block.script):
        raise ToolkitError(
            f"codec.dat block {block.index} has an invalid French procedure boundary."
        )
    final_english_index = struct.unpack_from("<I", block.script, boundary)[0]
    if final_english_index >= len(block.texts):
        raise ToolkitError(
            f"codec.dat block {block.index} has an invalid English text boundary."
        )
    return range(final_english_index + 1)


def build_codec_block(block: CodecBlock, replacements: dict[int, bytes] | None = None) -> bytes:
    replacements = replacements or {}
    out = bytearray()
    for value in block.header_words:
        out += struct.pack("<i", value)
    out += struct.pack("<i", -1)
    text_offset = 20 + len(block.texts) * 4
    text_blob = bytearray()
    table_values: list[int] = []
    for text in block.texts:
        table_values.append((text.table_value & 0x80000000) | len(text_blob))
        text_blob += replacements.get(text.index, text.data)
    padding = b"FON"
    padding_index = 0
    while len(text_blob) % 4:
        text_blob.append(padding[padding_index])
        padding_index += 1
    script_header_offset = text_offset + len(text_blob)
    script_offset = script_header_offset + len(block.script_header)
    out += struct.pack("<IIIII", script_offset, 20, text_offset, script_header_offset, 0)
    for value in table_values:
        out += struct.pack("<I", value)
    out += text_blob
    out += block.script_header
    out += block.script
    out += b"\0" * ((16 - len(out) % 16) % 16)
    # Keep an existing final read guard when re-exporting a patched image.
    out += bytes((len(block.script_padding) // 16) * 16)
    return bytes(out)


def codec_file_starts(data: bytes) -> list[int]:
    return _candidate_starts(data)[:-1]


def codec_rebuilt_layout(
    blocks: list[CodecBlock],
    replacements: dict[int, dict[int, bytes]] | None = None,
) -> tuple[list[int], list[int]]:
    replacements = replacements or {}
    starts: list[int] = []
    sizes: list[int] = []
    offset = 0
    for block in blocks:
        payload = build_codec_block(block, replacements.get(block.index))
        starts.append(offset)
        sizes.append(len(payload))
        offset += len(payload)
    return starts, sizes


def codec_slot_code(block_start: int, capacity_sectors: int) -> int:
    if block_start % 16:
        raise ToolkitError("Codec block start is not 16-byte aligned.")
    offset_field = block_start // 16
    if not 0 <= offset_field <= 0xFFFFF:
        raise ToolkitError("Rebuilt codec.dat exceeds its 20-bit offset limit.")
    if not 0 < capacity_sectors <= 0xFFF:
        raise ToolkitError("Rebuilt codec.dat page exceeds its 12-bit size limit.")
    return (capacity_sectors << 20) | offset_field


def build_codec(blocks: list[CodecBlock], replacements: dict[int, dict[int, bytes]] | None = None,
                *, read_capacities: dict[int, int] | None = None) -> bytes:
    replacements = replacements or {}
    rebuilt_data = b"".join(build_codec_block(block, replacements.get(block.index)) for block in blocks)
    if replacements and blocks:
        # Readers consume the slot's sector count, including alignment bytes.
        # Extend EOF to cover every caller's retained/expanded read window.
        starts, sizes = codec_rebuilt_layout(blocks, replacements)
        capacities = read_capacities or {}
        read_end = max(start + max(capacities.get(i, 16), (sizes[i]+2047)//2048)*2048
                       for i, start in enumerate(starts))
        needed = read_end - len(rebuilt_data)
        if needed > 0:
            rebuilt_data += bytes(needed)
    rebuilt_blocks = parse_codec(rebuilt_data)
    if len(rebuilt_blocks) != len(blocks):
        raise ToolkitError("Rebuilt codec.dat changed the block count.")
    for original, rebuilt in zip(blocks, rebuilt_blocks):
        if original.header_words != rebuilt.header_words:
            raise ToolkitError(f"Rebuilt codec.dat block {original.index} changed its procedure table.")
        if original.script_header != rebuilt.script_header:
            raise ToolkitError(f"Rebuilt codec.dat block {original.index} changed its script header.")
        if rebuilt.script != original.script:
            raise ToolkitError(f"Rebuilt codec.dat block {original.index} changed its script.")
        block_replacements = replacements.get(original.index, {})
        if len(original.texts) != len(rebuilt.texts):
            raise ToolkitError(f"Rebuilt codec.dat block {original.index} changed its text count.")
        for source_text, rebuilt_text in zip(original.texts, rebuilt.texts):
            expected = block_replacements.get(source_text.index, source_text.data)
            if rebuilt_text.data != expected:
                raise ToolkitError(
                    f"Rebuilt codec.dat block {original.index} text {source_text.index} failed verification."
                )
    return rebuilt_data
