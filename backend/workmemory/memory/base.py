from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class MemoryDocument:
    episode_id: UUID
    revision: int
    text: str
    reference_time: datetime
    project: str | None = None


@dataclass
class MemoryHit:
    episode_id: UUID
    score: float = 0.0


class MemoryAdapter(ABC):
    name: str

    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def close(self): ...

    @abstractmethod
    async def upsert(self, document: MemoryDocument) -> list[str]: ...

    @abstractmethod
    async def delete(self, episode_id: UUID) -> None: ...

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[MemoryHit]: ...

    async def correct(self, document: MemoryDocument) -> list[str]:
        return await self.upsert(document)

    async def rebuild(self, documents):
        for document in documents:
            await self.upsert(document)

    @abstractmethod
    async def status(self) -> dict: ...
