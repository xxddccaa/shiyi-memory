"""Models select whole source blocks; only the server may materialize their text."""
import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from workmemory.db.models import Activity, Observation
from workmemory.ingestion.dedup import restore
from workmemory.jobs.queue import now


class ExcerptSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_block_ids: list[str] = Field(default_factory=list, max_length=12)


@dataclass(frozen=True)
class SourceBlock:
    id: str
    source_id: str
    start: int
    end: int
    text: str
    captured_at: object
    expires_at: object
    app: str
    title: str
    source_hash: str
    method: str

    def model_view(self):
        return {"id": self.id, "text": self.text, "app": self.app,
                "title": self.title, "captured_at": self.captured_at.isoformat()}

    def provenance(self, revision):
        return {"source_id": self.source_id, "start": self.start, "end": self.end,
                "source_hash": self.source_hash, "revision": revision, "policy": 1,
                "captured_at": self.captured_at.isoformat(), "expires_at": self.expires_at.isoformat(),
                "app": self.app, "method": self.method}


async def source_blocks(session, episode_id, retention_days, budget=5500):
    rows = (await session.execute(select(Observation, Activity)
        .join(Activity, Observation.activity_id == Activity.id)
        .where(Observation.episode_id == episode_id, Observation.meaningful.is_(True))
        .order_by(Observation.captured_at.desc(), Observation.id))).all()
    observed = {a.id for _, a in rows}
    activities = (await session.scalars(select(Activity).where(Activity.episode_id == episode_id)
        .order_by(Activity.captured_at.desc(), Activity.id))).all()
    sources = [(f"raw:{o.id}", restore(a.text, o.delta) if a.text else None,
                o.captured_at, min(o.captured_at, a.captured_at), o.app, o.title, o.method)
               for o, a in rows]
    sources += [(f"raw:{a.id}", a.text, a.captured_at, a.captured_at, a.app, a.title, a.method)
                for a in activities if a.id not in observed]
    sources.sort(key=lambda s: s[2], reverse=True)
    blocks, used, seen = [], 0, set()
    for sid, text, at, base_at, app, title, method in sources:
        expiry = base_at + timedelta(days=retention_days)
        if not text or expiry <= now():
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        # Blank-line paragraphs retain line-level speaker/negation context. Include
        # the previous and next paragraph too; never manufacture a joined head/tail.
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.S)]
        for i, _ in enumerate(spans):
            start, end = spans[max(0, i - 1)][0], spans[min(len(spans) - 1, i + 1)][1]
            body = text[start:end]
            key = (sid, start, end)
            cost = len(body) + len(title) + len(app) + 180
            if key in seen or len(body) > 3000 or used + cost > budget:
                continue
            seen.add(key)
            blocks.append(SourceBlock(f"{sid}:{start}:{end}:{digest[:12]}", sid, start, end,
                                      body, at, expiry, app, title, digest, method))
            used += cost
    return blocks


def selected_blocks(selection, blocks):
    by_id = {block.id: block for block in blocks}
    if any(identifier not in by_id for identifier in selection.selected_block_ids):
        raise ValueError("Unknown excerpt source")
    result, seen = [], set()
    for identifier in selection.selected_block_ids:
        block = by_id[identifier]
        # Deduplicate complete identical excerpts, preserving the newest source.
        key = (block.app, block.text)
        if key not in seen:
            result.append(block)
            seen.add(key)
    return result


def summary_text(blocks):
    return "\n\n".join(f"{'记录原文' if b.method == 'manual' else '屏幕识别文字'}（{b.captured_at.isoformat()} · {b.app}）：\n{b.text}" for b in blocks)
