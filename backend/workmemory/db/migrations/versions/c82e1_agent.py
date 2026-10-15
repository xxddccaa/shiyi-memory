"""Canonical evidence, built-in agent conversations and derived work context.

Fixed DDL: this migration does not import live application metadata.
"""

from alembic import op

revision = "c82e1_agent"
down_revision = "53b0f151_obs"
branch_labels = depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE conversations (
            id UUID NOT NULL,
            title VARCHAR(200) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            expired BOOLEAN NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE derived_artifacts (
            id UUID NOT NULL,
            kind VARCHAR(20) NOT NULL,
            scope VARCHAR(100) NOT NULL,
            body JSONB,
            user_text TEXT,
            edit_revision INTEGER NOT NULL,
            needs_review BOOLEAN NOT NULL,
            status VARCHAR(20) NOT NULL,
            source_hash VARCHAR(64) NOT NULL,
            requested_hash VARCHAR(64) NOT NULL,
            attempts INTEGER NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (kind, scope)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE evidence_dependencies (
            consumer_kind VARCHAR(20) NOT NULL,
            consumer_id UUID NOT NULL,
            snippet_id VARCHAR(100) NOT NULL,
            source_kind VARCHAR(20) NOT NULL,
            source_id UUID NOT NULL,
            revision INTEGER NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (consumer_kind, consumer_id, snippet_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_evidence_dependencies_expires_at ON evidence_dependencies (expires_at)"
    )
    op.execute(
        "CREATE INDEX ix_evidence_dependencies_source_id ON evidence_dependencies (source_id)"
    )
    op.execute(
        """
        CREATE TABLE chat_messages (
            id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            role VARCHAR(20) NOT NULL,
            content TEXT,
            status VARCHAR(20) NOT NULL,
            citations JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_chat_messages_conversation_id ON chat_messages (conversation_id)")
    op.execute("CREATE INDEX ix_chat_messages_created_at ON chat_messages (created_at)")
    op.execute(
        """
        CREATE TABLE agent_runs (
            id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            message_id UUID NOT NULL,
            status VARCHAR(20) NOT NULL,
            error VARCHAR(500),
            model_calls INTEGER NOT NULL,
            search_calls INTEGER NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            finished_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE,
            FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_runs_conversation_id ON agent_runs (conversation_id)")
    op.execute(
        "CREATE UNIQUE INDEX ix_agent_runs_one_active ON agent_runs (conversation_id) WHERE status IN ('queued', 'running')"
    )
    op.execute(
        """
        CREATE TABLE memory_candidates (
            id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            message_id UUID NOT NULL,
            text TEXT,
            status VARCHAR(20) NOT NULL,
            revision INTEGER NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (message_id),
            FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE,
            FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_candidates_conversation_id ON memory_candidates (conversation_id)"
    )
    op.execute("CREATE INDEX ix_memory_candidates_message_id ON memory_candidates (message_id)")
    op.execute(
        """
        CREATE TABLE search_documents (
            id VARCHAR(100) NOT NULL,
            episode_id UUID NOT NULL,
            revision INTEGER NOT NULL,
            kind VARCHAR(20) NOT NULL,
            source_id UUID NOT NULL,
            captured_at TIMESTAMP WITH TIME ZONE NOT NULL,
            app VARCHAR(200),
            expires_at TIMESTAMP WITH TIME ZONE,
            tokens TSVECTOR NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(episode_id) REFERENCES episodes (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_search_documents_captured_at ON search_documents (captured_at)")
    op.execute("CREATE INDEX ix_search_documents_episode_id ON search_documents (episode_id)")
    op.execute("CREATE INDEX ix_search_documents_expires_at ON search_documents (expires_at)")
    op.execute("CREATE INDEX ix_search_documents_tokens ON search_documents USING gin (tokens)")
    op.execute(
        """
        CREATE TABLE work_facts (
            id UUID NOT NULL,
            episode_id UUID NOT NULL,
            revision INTEGER NOT NULL,
            kind VARCHAR(20) NOT NULL,
            text TEXT NOT NULL,
            state VARCHAR(20) NOT NULL,
            observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(episode_id) REFERENCES episodes (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_work_facts_episode_id ON work_facts (episode_id)")
    op.execute(
        """
        CREATE TABLE work_sessions (
            id UUID NOT NULL,
            episode_id UUID NOT NULL,
            revision INTEGER NOT NULL,
            title VARCHAR(300) NOT NULL,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL,
            ended_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (episode_id),
            FOREIGN KEY(episode_id) REFERENCES episodes (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_events (
            id SERIAL NOT NULL,
            run_id UUID NOT NULL,
            kind VARCHAR(30) NOT NULL,
            data JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_events_run_id ON agent_events (run_id)")
    op.execute(
        """
        CREATE TABLE todo_sources (
            todo_id UUID NOT NULL,
            episode_id UUID NOT NULL,
            revision INTEGER NOT NULL,
            PRIMARY KEY (todo_id, episode_id),
            FOREIGN KEY(todo_id) REFERENCES todos (id) ON DELETE CASCADE,
            FOREIGN KEY(episode_id) REFERENCES episodes (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("ALTER TABLE todos ADD COLUMN dismissed BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE todos ADD COLUMN normalized_key VARCHAR(64)")
    op.execute("CREATE INDEX ix_todos_normalized_key ON todos (normalized_key)")


def downgrade():
    op.drop_index("ix_todos_normalized_key", "todos")
    op.drop_column("todos", "normalized_key")
    op.drop_column("todos", "dismissed")
    op.drop_table("todo_sources")
    op.drop_table("agent_events")
    op.drop_table("work_sessions")
    op.drop_table("work_facts")
    op.drop_table("search_documents")
    op.drop_table("memory_candidates")
    op.drop_table("agent_runs")
    op.drop_table("chat_messages")
    op.drop_table("evidence_dependencies")
    op.drop_table("derived_artifacts")
    op.drop_table("conversations")
