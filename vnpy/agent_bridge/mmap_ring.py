"""Bounded single-producer/single-consumer mmap ring.

The hot path performs no network I/O. Separate instances are used for the
critical and routine lanes in each direction.
"""

from mmap import mmap
from pathlib import Path
from struct import Struct
from threading import Lock


_METADATA = Struct("<8sIII")
_HEADER_SIZE = 192
_WRITE_CURSOR_OFFSET = 64
_READ_CURSOR_OFFSET = 128
_CURSOR = Struct("<Q")
_SLOT_LENGTH = Struct("<I")
_MAGIC = b"ATRING1\0"


class RingFull(RuntimeError):
    pass


class MmapRing:
    def __init__(self, path: Path, capacity: int, slot_size: int = 4096) -> None:
        if capacity < 2 or slot_size < 64:
            raise ValueError("invalid ring dimensions")
        self.path = Path(path)
        self.capacity = capacity
        self.slot_size = slot_size
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        size = _HEADER_SIZE + capacity * slot_size
        is_new = not self.path.exists()
        with self.path.open("a+b") as file:
            file.truncate(size)
        self._file = self.path.open("r+b", buffering=0)
        self._map = mmap(self._file.fileno(), size)
        if is_new:
            self._write_header(0, 0)
        else:
            magic, version, stored_capacity, stored_slot_size = _METADATA.unpack_from(self._map)
            if magic != _MAGIC or version != 1 or stored_capacity != capacity or stored_slot_size != slot_size:
                self.close()
                raise ValueError("incompatible shared-memory ring")

    def _cursors(self) -> tuple[int, int]:
        return (
            _CURSOR.unpack_from(self._map, _WRITE_CURSOR_OFFSET)[0],
            _CURSOR.unpack_from(self._map, _READ_CURSOR_OFFSET)[0],
        )

    def _write_header(self, write: int, read: int) -> None:
        _METADATA.pack_into(self._map, 0, _MAGIC, 1, self.capacity, self.slot_size)
        _CURSOR.pack_into(self._map, _WRITE_CURSOR_OFFSET, write)
        _CURSOR.pack_into(self._map, _READ_CURSOR_OFFSET, read)

    def try_publish(self, payload: bytes) -> int:
        if len(payload) > self.slot_size - _SLOT_LENGTH.size:
            raise ValueError("payload exceeds ring slot")
        with self._lock:
            write, read = self._cursors()
            if write - read >= self.capacity:
                raise RingFull("shared-memory lane is full")
            offset = _HEADER_SIZE + (write % self.capacity) * self.slot_size
            _SLOT_LENGTH.pack_into(self._map, offset, len(payload))
            self._map[offset + _SLOT_LENGTH.size:offset + _SLOT_LENGTH.size + len(payload)] = payload
            _CURSOR.pack_into(self._map, _WRITE_CURSOR_OFFSET, write + 1)
            return write

    def try_consume(self) -> bytes | None:
        with self._lock:
            write, read = self._cursors()
            if read >= write:
                return None
            offset = _HEADER_SIZE + (read % self.capacity) * self.slot_size
            (length,) = _SLOT_LENGTH.unpack_from(self._map, offset)
            if length > self.slot_size - _SLOT_LENGTH.size:
                raise ValueError("corrupt ring slot")
            payload = bytes(self._map[offset + _SLOT_LENGTH.size:offset + _SLOT_LENGTH.size + length])
            _CURSOR.pack_into(self._map, _READ_CURSOR_OFFSET, read + 1)
            return payload

    def depth(self) -> int:
        write, read = self._cursors()
        return write - read

    def close(self) -> None:
        if getattr(self, "_map", None) is not None:
            self._map.close()
            self._map = None
        if getattr(self, "_file", None) is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "MmapRing":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
