from __future__ import annotations

import struct
from dataclasses import dataclass

from common import ToolkitError, align


@dataclass(frozen=True)
class CapSubtitle:
    index: int
    segment_index: int
    start_time: int
    end_time: int
    timing_flags: int
    text_flags: int
    data: bytes
    stored_size: int
    trailing: bytes
    raw_record: bytes


@dataclass(frozen=True)
class CapSegment:
    index: int
    header: bytes
    subtitles: list[CapSubtitle]
    record_bytes: int
    padding: bytes


@dataclass(frozen=True)
class CapFile:
    segments: list[CapSegment]
    subtitles: list[CapSubtitle]
    raw_data: bytes


def parse_cap(data: bytes) -> CapFile:
    if not data:
        return CapFile([], [], b"")
    if len(data) < 32:
        raise ToolkitError("CAP subtitle file is too small.")
    declared_size = struct.unpack_from("<I", data, 0)[0]
    if declared_size != len(data):
        raise ToolkitError("CAP subtitle header has invalid sizes.")

    segments: list[CapSegment] = []
    subtitles: list[CapSubtitle] = []
    segment_start = 0
    previous_start_time = 0
    while segment_start < len(data):
        if segment_start + 20 > len(data):
            raise ToolkitError("CAP segment header is truncated.")
        total_size, segment_size, carried_time, reserved, record_bytes = struct.unpack_from(
            "<IIIII", data, segment_start
        )
        if segment_start == 0:
            if total_size != len(data):
                raise ToolkitError("CAP file size does not match its header.")
        elif total_size != 0:
            raise ToolkitError("CAP continuation segment has an invalid file size field.")
        if carried_time != previous_start_time or reserved != 0:
            raise ToolkitError("CAP segment chain is invalid.")
        if (
            segment_size < 32
            or segment_size % 16
            or segment_start + segment_size > len(data)
            or 20 + record_bytes > segment_size
        ):
            raise ToolkitError("CAP segment has invalid boundaries.")

        segment_subtitles: list[CapSubtitle] = []
        position = segment_start + 20
        record_end = position + record_bytes
        while position < record_end:
            if position + 16 > record_end:
                raise ToolkitError("CAP subtitle timing record is truncated.")
            start_time, end_time, timing_flags = struct.unpack_from("<III", data, position)
            record_size, text_flags = struct.unpack_from("<HH", data, position + 12)
            stored_size = record_size - 12
            if record_size < 16 or record_size % 4 or position + record_size > record_end:
                raise ToolkitError("CAP subtitle record has invalid boundaries.")
            payload = data[position + 16:position + record_size]
            nul = payload.find(b"\0")
            if nul < 0:
                raise ToolkitError("CAP subtitle record is not NUL-terminated.")
            subtitle = CapSubtitle(
                index=len(subtitles),
                segment_index=len(segments),
                start_time=start_time,
                end_time=end_time,
                timing_flags=timing_flags,
                text_flags=text_flags,
                data=bytes(payload[:nul]),
                stored_size=stored_size,
                trailing=bytes(payload[nul + 1:]),
                raw_record=bytes(data[position:position + record_size]),
            )
            subtitles.append(subtitle)
            segment_subtitles.append(subtitle)
            previous_start_time = start_time
            position += record_size
        if position != record_end:
            raise ToolkitError("CAP subtitle records do not fill their declared segment data.")
        padding = bytes(data[record_end:segment_start + segment_size])
        segments.append(
            CapSegment(
                index=len(segments),
                header=bytes(data[segment_start:segment_start + 20]),
                subtitles=segment_subtitles,
                record_bytes=record_bytes,
                padding=padding,
            )
        )
        segment_start += segment_size
    if segment_start != len(data):
        raise ToolkitError("CAP segment chain does not end at the file boundary.")
    return CapFile(segments, subtitles, bytes(data))


def _split_timing(start_time: int, end_time: int, count: int, index: int) -> tuple[int, int]:
    duration = max(0, end_time - start_time)
    start = start_time + duration * index // count
    end = start_time + duration * (index + 1) // count
    return start, end


def _build_record(subtitle: CapSubtitle, text: bytes, page_count: int, page_index: int) -> bytes:
    if b"\0" in text:
        raise ToolkitError("CAP subtitle text cannot contain a NUL byte.")
    if page_count == 1 and text == subtitle.data:
        return subtitle.raw_record
    original_capacity = subtitle.stored_size if page_count == 1 else 4
    stored_size = max(original_capacity, align(4 + len(text) + 1, 4))
    record_size = 12 + stored_size
    if record_size > 0x7FFF:
        raise ToolkitError("A CAP subtitle exceeds the signed 16-bit record limit used by flashdemo.prx.")
    start_time, end_time = _split_timing(
        subtitle.start_time, subtitle.end_time, page_count, page_index
    )
    record = bytearray(
        struct.pack(
            "<IIIHH",
            start_time,
            end_time,
            subtitle.timing_flags,
            record_size,
            subtitle.text_flags,
        )
    )
    record += text + b"\0"
    record += b"\0" * (stored_size - 4 - len(text) - 1)
    return bytes(record)


def segment_size(segment: CapSegment) -> int:
    return len(segment.header) + segment.record_bytes + len(segment.padding)


def _pack_segments(blocks: list[list[tuple[int, bytes]]], original: CapFile) -> bytes:
    """Preserve record membership; each block declares its own size.

    EU flashdemo.prx 0xB4BC loads header+4 and advances by that value.
    0xB368 reads the block's record-byte count; there is no fixed-stride walk.
    Original files themselves mix 2064- and 2080-byte body blocks.
    """
    output = bytearray()
    carried = 0
    for segment, records in zip(original.segments, blocks):
        blob = b''.join(record for _start, record in records)
        # Preserve the complete original block when nothing in it changed.
        if blob == b''.join(sub.raw_record for sub in segment.subtitles):
            size = segment_size(segment)
            padding = segment.padding
        else:
            # Retail uses 1..16 trailing bytes even when the body is aligned.
            size = (20 + len(blob) + 16) & ~15
            padding = bytes(size - 20 - len(blob))
        output += struct.pack('<IIIII', 0, size, carried, 0, len(blob))
        output += blob + padding
        if records:
            carried = records[-1][0]
    if output:
        struct.pack_into('<I', output, 0, len(output))
    return bytes(output)


def build_cap(parsed: CapFile, replacements: dict[int, bytes | list[bytes]] | None = None) -> bytes:
    if not parsed.segments:
        return b""
    replacements = replacements or {}
    if not replacements:
        return parsed.raw_data

    valid = {sub.index for sub in parsed.subtitles}
    unknown = set(replacements) - valid
    if unknown:
        raise ToolkitError(f"Unknown CAP subtitle index: {min(unknown)}")
    blocks = []
    for segment in parsed.segments:
        records: list[tuple[int, bytes]] = []
        for subtitle in segment.subtitles:
            replacement = replacements.get(subtitle.index, subtitle.data)
            pages = replacement if isinstance(replacement, list) else [replacement]
            if not pages:
                pages = [b'']
            for page_index, text in enumerate(pages):
                record = _build_record(subtitle, text, len(pages), page_index)
                records.append((struct.unpack_from('<I', record, 0)[0], record))
        blocks.append(records)
    output = _pack_segments(blocks, parsed)
    rebuilt = parse_cap(output)
    expected_count = len(parsed.subtitles) + sum(
        max(1, len(value)) - 1
        for value in replacements.values()
        if isinstance(value, list)
    )
    if len(rebuilt.subtitles) != expected_count:
        raise ToolkitError("Rebuilt CAP changed its subtitle record count unexpectedly.")
    return output
