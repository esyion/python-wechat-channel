"""Store interface + two implementations — mirrors src/store/."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class Store(ABC):
    """Key-value persistence for channel state."""

    @abstractmethod
    async def get(self, key: str) -> Optional[str]: ...

    @abstractmethod
    async def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def flush(self) -> None: ...


class MemoryStore(Store):
    """
    In-process dict-backed Store.

    Data does not survive process restart. ``flush()`` is a no-op.
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._map.get(key)

    async def set(self, key: str, value: str) -> None:
        self._map[key] = value

    async def delete(self, key: str) -> None:
        self._map.pop(key, None)

    async def flush(self) -> None:
        pass  # No-op


class JsonFileStore(Store):
    """
    JSON-file-backed Store with atomic-ish writes via a .tmp swap.

    On first access, loads the entire file into memory; subsequent reads are
    served from the in-memory map. Writes are kept in memory and flushed to
    disk asynchronously.

    Tolerant of ENOENT (returns empty store on first run).
    """

    def __init__(self, file_path: str) -> None:
        self._path = Path(file_path)
        self._state: dict[str, str] = {}
        self._loaded = False
        self._loading: Optional[asyncio.Task[None]] = None
        self._writing: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._writing.set_result(None)  # Start as resolved

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._loading is None:
            self._loading = asyncio.create_task(self._do_load())
        await self._loading

    async def _do_load(self) -> None:
        try:
            raw = self._path.read_text("utf-8")
            parsed = json.loads(raw)
            self._state = dict(parsed.get("data", {}))
        except FileNotFoundError:
            self._state = {}
        except json.JSONDecodeError:
            self._state = {}
        self._loaded = True
        self._loading = None

    def _serialize(self) -> str:
        return json.dumps({"data": self._state}, ensure_ascii=False)

    async def get(self, key: str) -> Optional[str]:
        await self._ensure_loaded()
        return self._state.get(key)

    async def set(self, key: str, value: str) -> None:
        await self._ensure_loaded()
        self._state[key] = value
        # Chain onto existing write
        prev = self._writing

        async def persist():
            await prev
            await self._do_persist()

        self._writing = asyncio.create_task(persist())
        await self._writing

    async def delete(self, key: str) -> None:
        await self._ensure_loaded()
        self._state.pop(key, None)
        prev = self._writing

        async def persist():
            await prev
            await self._do_persist()

        self._writing = asyncio.create_task(persist())
        await self._writing

    async def flush(self) -> None:
        await self._writing

    async def _do_persist(self) -> None:
        # Ensure parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(self._serialize(), "utf-8")
        tmp.replace(self._path)  # atomic on POSIX
