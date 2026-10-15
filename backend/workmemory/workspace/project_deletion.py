"""Delete project groupings while preserving independently owned work and reminders."""

import uuid

from sqlalchemy import delete, select, update

from workmemory.db.models import (
    DerivedArtifact,
    Episode,
    MemoryCandidate,
    ProjectAlias,
    Todo,
    TodoTombstone,
)
from workmemory.evidence.service import invalidate_memory, invalidate_object

ACTIONS = {"project.delete", "project.delete_many"}


async def impact(session, projects):
    """The exact related versions form part of the confirmation snapshot."""
    ids = [p.id for p in projects]
    result = {p.id: {"episode": [], "todo": [], "memory": [], "review": []} for p in projects}
    for model, kind, version in (
        (Episode, "episode", Episode.revision),
        (Todo, "todo", Todo.revision),
    ):
        for identifier, revision, project_id in (await session.execute(
            select(model.id, version, model.project_id).where(model.project_id.in_(ids)).order_by(model.id)
        )).all():
            result[project_id][kind].append([str(identifier), revision])
    strings = [str(identifier) for identifier in ids]
    for identifier, revision, project_id in (await session.execute(
        select(MemoryCandidate.id, MemoryCandidate.revision, MemoryCandidate.origin["project_id"].astext)
        .where(MemoryCandidate.origin["project_id"].astext.in_(strings)).order_by(MemoryCandidate.id)
    )).all():
        result[uuid.UUID(project_id)]["memory"].append([str(identifier), revision])
    for identifier, revision, scope, source_hash, requested_hash in (await session.execute(
        select(DerivedArtifact.id, DerivedArtifact.edit_revision, DerivedArtifact.scope,
               DerivedArtifact.source_hash, DerivedArtifact.requested_hash)
        .where(DerivedArtifact.kind == "brief", DerivedArtifact.scope.in_(strings)).order_by(DerivedArtifact.id)
    )).all():
        result[uuid.UUID(scope)]["review"].append([str(identifier), revision, source_hash, requested_hash])
    aliases = {identifier: [] for identifier in ids}
    for name, project_id in (await session.execute(
        select(ProjectAlias.name, ProjectAlias.project_id).where(ProjectAlias.project_id.in_(ids)).order_by(ProjectAlias.name)
    )).all():
        aliases[project_id].append(name)
    return result, aliases


def confirmation_locks(preview):
    return {
        (kind, uuid.UUID(value[0]))
        for item in (preview or {}).get("before", [])
        for kind, values in item.get("linked", {}).items()
        for value in values
    }


async def remove_project(session, project):
    from workmemory.artifacts.service import todo_key
    from workmemory.workspace.operations import move_episode

    await invalidate_object(session, "project", project.id)
    episodes = (await session.scalars(select(Episode).where(Episode.project_id == project.id).order_by(Episode.id))).all()
    for episode in episodes:
        if episode.deleted_at:
            episode.project_id, episode.project_hint = None, None
            episode.revision += 1
        else:
            await move_episode(session, episode, None)
    for todo in (await session.scalars(select(Todo).where(Todo.project_id == project.id).order_by(Todo.id))).all():
        await invalidate_object(session, "todo", todo.id)
        todo.project_id = None
        todo.normalized_key = todo_key(todo.text, None)
        todo.revision += 1
    for memory in (await session.scalars(select(MemoryCandidate).where(
        MemoryCandidate.origin["project_id"].astext == str(project.id)
    ).order_by(MemoryCandidate.id))).all():
        await invalidate_memory(session, memory.id)
        memory.origin = {**memory.origin, "project_id": None}
        memory.revision += 1
    for report in (await session.scalars(select(DerivedArtifact).where(
        DerivedArtifact.kind == "brief", DerivedArtifact.scope == str(project.id)
    ).order_by(DerivedArtifact.id))).all():
        await invalidate_object(session, "review", report.id)
        report.body, report.status = None, "invalidated"
        report.source_hash = report.requested_hash = ""
        report.needs_review = bool(report.user_text)
        report.edit_revision += 1
    await session.execute(delete(ProjectAlias).where(ProjectAlias.project_id == project.id))
    await session.execute(update(TodoTombstone).where(TodoTombstone.project_id == project.id).values(project_id=None))
    # Keep the unique name as a suppression marker: delayed imports cannot recreate it.
    project.state, project.revision = "deleted", project.revision + 1
