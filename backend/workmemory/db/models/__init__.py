import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Episode(Base):
    __tablename__ = "episodes"
    context_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    evidence_state: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    evidence_policy: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    evidence_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_reviewed_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    project_hint: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    app: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(1000))
    text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint("episode_id", "content_hash"),)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    activity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("activities.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    app: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(1000), default="")
    window_id: Mapped[str] = mapped_column(String(200), default="")
    interaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    trigger: Mapped[str | None] = mapped_column(String(30))
    phase: Mapped[str | None] = mapped_column(String(20))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(String(10))
    delta: Mapped[dict | None] = mapped_column(JSONB)
    similarity: Mapped[float] = mapped_column(Float, default=0)
    meaningful: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(String(30), default="new")
    capture_meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        Index("ix_observations_scope", "episode_id", "app", "window_id", "captured_at"),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(String(220), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_jobs_claim", "status", "available_at", "priority"),)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)


class MemoryBackend(Base):
    __tablename__ = "memory_backends"
    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemoryReceipt(Base):
    __tablename__ = "memory_receipts"
    backend: Mapped[str] = mapped_column(ForeignKey("memory_backends.name"), primary_key=True)
    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    external_ids: Mapped[list] = mapped_column(JSONB, default=list)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Todo(Base):
    __tablename__ = "todos"
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    text: Mapped[str] = mapped_column(String(1000))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    origin: Mapped[str] = mapped_column(String(20), default="legacy", server_default="legacy")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai", server_default="Asia/Shanghai")
    repeat: Mapped[str] = mapped_column(String(20), default="once", server_default="once")
    weekdays: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    local_time: Mapped[str | None] = mapped_column(String(8))
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notify_pet: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    schedule_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    normalized_key: Mapped[str | None] = mapped_column(String(64), index=True)
    __table_args__ = (UniqueConstraint("episode_id", "text"),)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))
    episode_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    todo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("todos.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    schedule_revision: Mapped[int | None] = mapped_column(Integer)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    shown_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    missed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"))


class SearchDocument(Base):
    """Only the token index is duplicated; evidence text is always read from its source."""

    __tablename__ = "search_documents"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    app: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Populated with Chinese tokens using PostgreSQL's simple text configuration.
    from sqlalchemy.dialects.postgresql import TSVECTOR

    tokens: Mapped[str] = mapped_column(TSVECTOR)
    __table_args__ = (Index("ix_search_documents_tokens", "tokens", postgresql_using="gin"),)


class Conversation(Base):
    __tablename__ = "conversations"
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_workspaces.id", ondelete="SET NULL"))
    coding_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expired: Mapped[bool] = mapped_column(Boolean, default=False)


class ChatMessage(Base):
    context: Mapped[dict | None] = mapped_column(JSONB)
    external_citations: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    __tablename__ = "chat_messages"
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (UniqueConstraint("conversation_id", "request_id", name="uq_chat_request"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str | None] = mapped_column(String(500))
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    search_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    from sqlalchemy import text as sql_text

    __table_args__ = (
        Index(
            "ix_agent_runs_one_active",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("status IN ('queued', 'running')"),
        ),
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryCandidate(Base):
    __tablename__ = "memory_candidates"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    source_episode_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("message_id"),)


class EvidenceDependency(Base):
    """All evidence seen by a consumer, including inherited chat context, not just citations."""

    __tablename__ = "evidence_dependencies"
    consumer_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    consumer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    snippet_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class WorkFact(Base):
    __tablename__ = "work_facts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="observed")
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkSession(Base):
    __tablename__ = "work_sessions"
    title_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    edit_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    category: Mapped[str] = mapped_column(String(50), default="工作", server_default="工作")
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), unique=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DerivedArtifact(Base):
    __tablename__ = "derived_artifacts"
    processed_sources: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_sources: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str] = mapped_column(String(100))
    body: Mapped[dict | None] = mapped_column(JSONB)
    user_text: Mapped[str | None] = mapped_column(Text)
    edit_revision: Mapped[int] = mapped_column(Integer, default=0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    requested_hash: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("kind", "scope"),)


class TodoSource(Base):
    __tablename__ = "todo_sources"
    todo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("todos.id", ondelete="CASCADE"), primary_key=True
    )
    episode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer)


class ClientEvent(Base):
    __tablename__ = "client_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    origin: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    preview: Mapped[dict | None] = mapped_column(JSONB)
    target_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ConversationDigest(Base):
    __tablename__ = "conversation_digests"
    partial_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    partial_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), unique=True
    )
    content: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectAlias(Base):
    __tablename__ = "project_aliases"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_kind: Mapped[str] = mapped_column(String(20))
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    value: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("object_kind", "object_id", "revision"),)


class TodoMergeRejection(Base):
    __tablename__ = "todo_merge_rejections"
    left_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("todos.id", ondelete="CASCADE"), primary_key=True
    )
    right_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("todos.id", ondelete="CASCADE"), primary_key=True
    )


class TodoTombstone(Base):
    __tablename__ = "todo_tombstones"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExternalSource(Base):
    __tablename__ = "external_sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000))
    content: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AgentWorkspace(Base):
    __tablename__ = "agent_workspaces"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text, unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    extensions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")


class PiSession(Base):
    __tablename__ = "pi_sessions"
    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict | None] = mapped_column(JSONB)
    epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), default="ready", server_default="ready")
    context: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="running")
    side_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("run_id", "call_id"),)


class AgentToolOutput(Base):
    __tablename__ = "agent_tool_output"
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tool_calls.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text)


class ProactiveUtterance(Base):
    trigger: Mapped[str] = mapped_column(String(30), default="timer", server_default="timer")
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    """Short-lived Pi runs; become chat messages only after an explicit click."""
    __tablename__ = "proactive_utterances"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    topic: Mapped[str] = mapped_column(String(20))
    preview: Mapped[bool] = mapped_column(Boolean, default=False)
    settings_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSONB)
    checkpoint: Mapped[dict | None] = mapped_column(JSONB)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"))


class EvidenceArchive(Base):
    """Human-only legacy display snapshots, never an agent evidence source."""
    __tablename__ = "evidence_archives"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_kind: Mapped[str] = mapped_column(String(20))
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("object_kind", "object_id", "revision"),)


class MaintenanceTask(Base):
    __tablename__ = "maintenance_tasks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(80))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaptureIncident(Base):
    __tablename__ = "capture_incidents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reason: Mapped[str] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
