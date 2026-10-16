"""Explicit report generation from a fixed set of confirmed source versions."""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from workmemory.artifacts.service import ArtifactBody, artifact_sources, artifact_view
from workmemory.db.models import AppState, DerivedArtifact, MemoryCandidate, Project
from workmemory.evidence.service import (
    dependencies,
    fingerprint,
    remember,
    resolve,
    valid_dependencies,
)
from workmemory.jobs.queue import enqueue
from workmemory.workspace.grouping import DAILY_WORK_LABEL


async def report(runtime, kind, scope):
    async with runtime.sessions.begin() as session:
        await session.execute(
            insert(DerivedArtifact)
            .values(id=uuid.uuid4(), kind=kind, scope=scope)
            .on_conflict_do_nothing()
        )
        row = await session.scalar(
            select(DerivedArtifact).where(
                DerivedArtifact.kind == kind, DerivedArtifact.scope == scope
            )
        )
        sources, _, total = await artifact_sources(
            session, kind, scope, runtime.settings.timezone, runtime.settings.retention_days
        )
        result = artifact_view(row)
        old = await dependencies(session, "artifact", row.id)
        if old and not await valid_dependencies(
            session, old, retention_days=runtime.settings.retention_days
        ):
            row.body, row.needs_review = None, bool(row.user_text)
            result.update(user_text=None, needs_review=row.needs_review)
        result["status"] = "ready"
        result["body"] = ArtifactBody(
            observations=[item for source in sources for item in source.get("observations", [])]
        ).model_dump()
        result["body"]["coverage"] = {
            "total": total,
            "source_count": len(sources),
            "truncated": False,
        }
        materials = [
            {**source, "selected": True}
            for source in sources
            if source.get("evidence_kind") != "screen_text"
        ]
        first = date.fromisoformat(scope)
        end = first + timedelta(days=7 if kind == "weekly" else 1)
        memories = (
            await session.scalars(
                select(MemoryCandidate)
                .where(MemoryCandidate.status == "confirmed")
                .order_by(MemoryCandidate.created_at.desc())
            )
        ).all()
        for memory in memories:
            origin = memory.origin or {}
            when = (
                date.fromisoformat(origin["date"])
                if origin.get("date")
                else memory.created_at.astimezone(ZoneInfo(runtime.settings.timezone)).date()
            )
            if first <= when < end:
                value = await resolve(session, f"memory:{memory.id}")
                if value:
                    materials.append(
                        {
                            **value,
                            "project_id": origin.get("project_id"),
                            "selected": bool(memory.source_episode_id),
                        }
                    )
        projects = {str(p.id): p.name for p in (await session.scalars(select(Project).where(Project.state.notin_(["merged", "deleted"])))).all()}
        for value in materials:
            value["project_name"] = projects.get(value.get("project_id"), DAILY_WORK_LABEL)
        result["materials"] = materials
        result["observation_groups"] = [
            {
                "project_name": projects.get(source.get("project_id"), DAILY_WORK_LABEL),
                "items": source.get("observations", []),
            }
            for source in sources
            if source.get("observations")
        ]
        draft = await session.scalar(
            select(DerivedArtifact).where(
                DerivedArtifact.kind == "report_draft", DerivedArtifact.scope == str(row.id)
            )
        )
        if draft and not await valid_dependencies(
            session, await dependencies(session, "artifact", draft.id)
        ):
            draft.body, draft.status = None, "invalidated"
        result["draft"] = artifact_view(draft) if draft else None
        return result


class Material(BaseModel):
    id: str
    revision: int = Field(ge=1)


class DraftInput(BaseModel):
    request_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    materials: list[Material] = Field(min_length=1, max_length=2000)


async def request_draft(runtime, identifier, body):
    async with runtime.sessions.begin() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": body.request_id.int % (2**63 - 1)}
        )
        request_key = f"report_draft_request:{body.request_id}"
        request_hash = fingerprint([str(identifier), body.model_dump_json()])
        prior = await session.get(AppState, request_key)
        if prior:
            if prior.value["request_hash"] != request_hash:
                raise HTTPException(409, "请求标识已经用于不同材料")
            draft = await session.get(DerivedArtifact, uuid.UUID(prior.value["draft_id"]))
            if not draft or draft.requested_hash != prior.value["hash"]:
                raise HTTPException(409, "这次请求已被新草稿替代，请刷新")
            return artifact_view(draft)
        sources = []
        for item in body.materials:
            value = await resolve(session, item.id)
            if (
                not value
                or value["revision"] != item.revision
                or (
                    value.get("source_kind") == "episode"
                    and value.get("evidence_kind") != "user_confirmed"
                )
                or value.get("source_kind") not in {"episode", "memory"}
            ):
                raise HTTPException(409, "材料已变化或未经确认，请重新选择")
            sources.append(value)
        if not await valid_dependencies_from_sources(session, sources):
            raise HTTPException(409, "材料已变化")
        parent = await session.scalar(
            select(DerivedArtifact).where(DerivedArtifact.id == identifier).with_for_update()
        )
        if not parent or parent.kind not in {"review", "weekly"}:
            raise HTTPException(404, "报告不存在")
        if parent.edit_revision != body.expected_revision:
            raise HTTPException(409, "人工稿已更新，请重新核对")
        await session.execute(
            insert(DerivedArtifact)
            .values(id=uuid.uuid4(), kind="report_draft", scope=str(identifier))
            .on_conflict_do_nothing()
        )
        draft = await session.scalar(
            select(DerivedArtifact)
            .where(DerivedArtifact.kind == "report_draft", DerivedArtifact.scope == str(identifier))
            .with_for_update()
        )
        key = fingerprint([str(body.request_id), *[f"{v['id']}:{v['revision']}" for v in sources]])
        if draft.requested_hash == key:
            return artifact_view(draft)
        # A regenerated draft never writes the parent/user_text.
        from sqlalchemy import delete

        from workmemory.db.models import EvidenceDependency

        await session.execute(
            delete(EvidenceDependency).where(
                EvidenceDependency.consumer_kind == "artifact",
                EvidenceDependency.consumer_id == draft.id,
            )
        )
        await remember(session, "artifact", draft.id, sources)
        draft.body, draft.status, draft.requested_hash = None, "pending", key
        session.add(
            AppState(
                key=request_key,
                value={"request_hash": request_hash, "draft_id": str(draft.id), "hash": key},
            )
        )
        await enqueue(
            session,
            "draft",
            f"draft:{body.request_id}",
            {"id": str(draft.id), "hash": key, "kind": parent.kind},
            priority=10,
        )
        return artifact_view(draft)


async def valid_dependencies_from_sources(session, sources):
    # Resolve again under source locks using the same ordering as deletion.
    from types import SimpleNamespace

    return await valid_dependencies(
        session,
        [
            SimpleNamespace(
                snippet_id=s["id"],
                source_kind=s["source_kind"],
                source_id=uuid.UUID(s["source_id"]),
                revision=s["revision"],
                expires_at=datetime.fromisoformat(s["expires_at"]) if s.get("expires_at") else None,
            )
            for s in sources
        ],
        lock=True,
    )


async def generate(runtime, payload):
    identifier = uuid.UUID(payload["id"])
    async with runtime.sessions.begin() as session:
        row = await session.get(DerivedArtifact, identifier)
        if not row or row.requested_hash != payload["hash"]:
            return
        deps = await dependencies(session, "artifact", identifier)
        if not await valid_dependencies(session, deps):
            row.status, row.body = "invalidated", None
            return
        sources = [await resolve(session, dep.snippet_id) for dep in deps]
        row.status = "running"
    try:
        from workmemory.artifacts.hierarchy import build_confirmed

        async def progress(*args):
            pass

        body = await build_confirmed(runtime, payload["kind"], sources, progress)
        async with runtime.sessions.begin() as session:
            valid = await valid_dependencies(session, deps, lock=True)
            row = await session.scalar(
                select(DerivedArtifact).where(DerivedArtifact.id == identifier).with_for_update()
            )
            if row.requested_hash == payload["hash"]:
                row.body, row.status = (body, "ready") if valid else (None, "invalidated")
                row.source_hash = payload["hash"] if valid else ""
                row.needs_review = not valid
    except Exception:
        async with runtime.sessions.begin() as session:
            row = await session.get(DerivedArtifact, identifier)
            if row.requested_hash == payload["hash"]:
                row.status = "failed" if row.status != "invalidated" else "invalidated"
        raise
