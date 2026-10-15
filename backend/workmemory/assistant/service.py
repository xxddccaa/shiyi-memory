import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from workmemory.config import ROOT
from workmemory.db.models import Episode, Project
from workmemory.evidence.policy import metadata, valid_excerpt_sources


class Answer(BaseModel):
    answer: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(max_length=10)


def episode_view(episode, project=None):
    return {
        **metadata(episode),
        "id": str(episode.id),
        "started_at": episode.started_at,
        "ended_at": episode.ended_at,
        "summary": episode.summary if metadata(episode)["evidence_state"] not in {"expired", "legacy_unverified"} else None,
        "status": episode.status,
        "revision": episode.revision,
        "corrected": episode.corrected,
        "project_id": str(episode.project_id) if episode.project_id else None,
        "project": project.name if project else episode.project_hint,
    }


async def retrieve(runtime, query, limit=10, **filters):
    from workmemory.evidence.service import search

    result = await search(runtime, query, limit=limit * 3, include_raw=False, **filters)
    ids = list(dict.fromkeys(s["episode_id"] for s in result["items"] if s["episode_id"]))[:limit]
    async with runtime.sessions() as session:
        rows = (await session.execute(select(Episode, Project).outerjoin(Project, Episode.project_id == Project.id)
            .where(Episode.id.in_([uuid.UUID(i) for i in ids]), Episode.status == "ready",
                   Episode.deleted_at.is_(None)))).all()
        by_id = {str(e.id): episode_view(e, p) for e, p in rows}
        return [by_id[i] for i in ids if i in by_id], result["warning"]


async def answer_question(runtime, question):
    sources, warning = await retrieve(runtime, question, 8)
    if not sources:
        return {
            "answer": "现有记录不足以确认。可以先添加工作记录，或等待当前片段整理完成。",
            "sources": [],
            "warning": warning,
        }
    import json

    evidence = [
        {
            "id": s["id"],
            "time": s["started_at"].isoformat(),
            "project": s["project"],
            "text": s["summary"],
        }
        for s in sources
    ]
    response = await runtime.ollama.chat(
        [
            {"role": "system", "content": (ROOT / "config/prompts/answer.txt").read_text()},
            {
                "role": "user",
                "content": json.dumps(
                    {"question": question, "evidence": evidence}, ensure_ascii=False
                ),
            },
        ],
        schema=Answer.model_json_schema(),
        priority=0,
    )
    result = Answer.model_validate_json(response)
    allowed_ids = {source["id"]: source for source in sources}
    if not result.evidence_ids or any(i not in allowed_ids for i in result.evidence_ids):
        return {
            "answer": "现有记录不足以确认，模型没有提供有效的证据引用。",
            "sources": [],
            "warning": warning,
        }
    # Deletion/correction during an in-flight model call invalidates the whole answer.
    async with runtime.sessions() as session:
        current = (
            await session.scalars(
                select(Episode).where(Episode.id.in_([uuid.UUID(s["id"]) for s in sources]))
            )
        ).all()
        valid = all([await valid_excerpt_sources(session, e, runtime.settings.retention_days) for e in current])
    if not valid or any(
        e.deleted_at or e.revision != allowed_ids[str(e.id)]["revision"] for e in current
    ) or len(current) != len(sources):
        return {
            "answer": "相关记录刚刚发生更新，请重新提问以使用最新证据。",
            "sources": [],
            "warning": warning,
        }
    return {
        "answer": result.answer,
        "sources": [allowed_ids[i] for i in dict.fromkeys(result.evidence_ids)],
        "warning": warning,
    }
