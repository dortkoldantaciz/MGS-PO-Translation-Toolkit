from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from common import ToolkitError


SECTOR_SIZE = 0x800
PVD_SECTOR = 16
VOLUME_ID = b"CD001"


@dataclass(frozen=True)
class IsoFile:
    path: str
    extent: int
    size: int
    record_offset: int


def _read_both_u32(data: bytes, offset: int) -> int:
    little = struct.unpack_from("<I", data, offset)[0]
    big = struct.unpack_from(">I", data, offset + 4)[0]
    if little != big:
        raise ToolkitError("The ISO contains an invalid dual-endian number.")
    return little


class PspIso:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file():
            raise ToolkitError(f"ISO file not found: {self.path}")
        self.files: dict[str, IsoFile] = {}
        self._descriptor_offsets: list[int] = []
        self._extent_starts: set[int] = set()
        self._read_index()

    def _read_index(self) -> None:
        with self.path.open("rb") as stream:
            descriptor = PVD_SECTOR * SECTOR_SIZE
            root: tuple[int, int] | None = None
            while True:
                stream.seek(descriptor)
                header = stream.read(SECTOR_SIZE)
                if len(header) != SECTOR_SIZE or header[1:6] != VOLUME_ID:
                    raise ToolkitError("The selected file is not a supported ISO9660 image.")
                kind = header[0]
                if kind in (1, 2):
                    self._descriptor_offsets.append(descriptor)
                if kind == 1:
                    record = header[156:]
                    root = (_read_both_u32(record, 2), _read_both_u32(record, 10))
                if kind == 255:
                    break
                descriptor += SECTOR_SIZE
            if root is None:
                raise ToolkitError("The ISO has no primary volume descriptor.")
            self._walk_directory(stream, "", root[0], root[1], set())

    def _walk_directory(
        self,
        stream,
        parent: str,
        extent: int,
        size: int,
        visited: set[tuple[int, int]],
    ) -> None:
        key = (extent, size)
        if key in visited:
            return
        visited.add(key)
        self._extent_starts.add(extent)
        stream.seek(extent * SECTOR_SIZE)
        data = stream.read(size)
        position = 0
        while position < len(data):
            length = data[position]
            if length == 0:
                position = ((position // SECTOR_SIZE) + 1) * SECTOR_SIZE
                continue
            if position + length > len(data) or length < 34:
                raise ToolkitError("The ISO directory table is malformed.")
            record = data[position:position + length]
            name_length = record[32]
            raw_name = record[33:33 + name_length]
            record_offset = extent * SECTOR_SIZE + position
            position += length
            if raw_name in (b"\x00", b"\x01"):
                continue
            name = raw_name.decode("ascii", errors="strict")
            version = name.find(";")
            if version >= 0:
                name = name[:version]
            path = f"{parent}/{name}" if parent else name
            child_extent = _read_both_u32(record, 2)
            child_size = _read_both_u32(record, 10)
            if record[25] & 0x02:
                self._walk_directory(stream, path, child_extent, child_size, visited)
            else:
                self._extent_starts.add(child_extent)
                self.files[path.upper()] = IsoFile(path, child_extent, child_size, record_offset)

    def get(self, path: str) -> IsoFile:
        normalized = path.replace("\\", "/").strip("/").upper()
        try:
            return self.files[normalized]
        except KeyError as exc:
            raise ToolkitError(f"The ISO does not contain {path}.") from exc

    def read_file(self, path: str) -> bytes:
        item = self.get(path)
        with self.path.open("rb") as stream:
            stream.seek(item.extent * SECTOR_SIZE)
            data = stream.read(item.size)
        if len(data) != item.size:
            raise ToolkitError(f"The ISO file is truncated: {item.path}")
        return data

    def _in_place_capacity(self, item: IsoFile) -> int:
        next_extent = min(
            (
                extent
                for extent in self._extent_starts
                if extent > item.extent
            ),
            default=(self.path.stat().st_size + SECTOR_SIZE - 1) // SECTOR_SIZE,
        )
        return max(item.size, (next_extent - item.extent) * SECTOR_SIZE)

    def extract_game(self, destination: Path) -> Path:
        destination = destination.resolve()
        wanted_files = {
            "PSP_GAME/SYSDIR/BOOT.BIN",
            "PSP_GAME/SYSDIR/EBOOT.BIN",
        }
        prefix = "PSP_GAME/USRDIR/"
        found = 0
        for item in self.files.values():
            normalized = item.path.upper()
            if not normalized.startswith(prefix.upper()) and normalized not in wanted_files:
                continue
            target = destination / Path(item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read_file(item.path))
            if normalized.startswith(prefix.upper()):
                found += 1
        if not found:
            raise ToolkitError("The ISO does not contain PSP_GAME/USRDIR files.")
        return destination

    def extract_usrdir(self, destination: Path) -> Path:
        return self.extract_game(destination)

    def rebuild(self, destination: Path, replacements: dict[str, bytes]) -> Path:
        destination = destination.resolve()
        if destination == self.path:
            raise ToolkitError("The rebuilt ISO must not overwrite the source ISO.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, destination)
        with destination.open("r+b") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            for name, payload in replacements.items():
                item = self.get(name)
                in_place_capacity = self._in_place_capacity(item)
                if len(payload) <= in_place_capacity:
                    extent = item.extent
                    stream.seek(extent * SECTOR_SIZE)
                    stream.write(payload)
                    if len(payload) < item.size:
                        stream.write(b"\0" * (item.size - len(payload)))
                else:
                    end = (end + SECTOR_SIZE - 1) & ~(SECTOR_SIZE - 1)
                    extent = end // SECTOR_SIZE
                    stream.seek(end)
                    stream.write(payload)
                    end += len(payload)
                stream.seek(item.record_offset + 2)
                stream.write(struct.pack("<I", extent))
                stream.write(struct.pack(">I", extent))
                stream.seek(item.record_offset + 10)
                stream.write(struct.pack("<I", len(payload)))
                stream.write(struct.pack(">I", len(payload)))
            stream.seek(0, 2)
            final_size = stream.tell()
            sectors = (final_size + SECTOR_SIZE - 1) // SECTOR_SIZE
            padded_size = sectors * SECTOR_SIZE
            if final_size != padded_size:
                stream.write(b"\0" * (padded_size - final_size))
            for offset in self._descriptor_offsets:
                stream.seek(offset + 80)
                stream.write(struct.pack("<I", sectors))
                stream.write(struct.pack(">I", sectors))
        rebuilt = PspIso(destination)
        for name, expected in replacements.items():
            actual = rebuilt.read_file(name)
            if actual != expected:
                raise ToolkitError(f"Rebuilt ISO verification failed for {name}.")
        return destination


def is_iso_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in (".iso", ".cso")
