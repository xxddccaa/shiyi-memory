"""Opt-in selected generation service; synthetic records in isolated PostgreSQL only."""
import os

import pytest
from sqlalchemy import select
from test_evidence_policy import pending
from workmemory.config.generation import GenerationStore
from workmemory.db.models import Episode, Project, WorkFact
from workmemory.episodes.service import summarize_job
from workmemory.evidence.policy import valid_excerpt_sources
from workmemory.inference.ollama import Ollama

pytestmark = pytest.mark.skipif(os.environ.get("WORKMEMORY_EVIDENCE_LIVE") != "1", reason="Explicit synthetic model verification")


@pytest.mark.parametrize("source,expected", [
    ("合成系统日志：2026-10-12 14:03:00，接口 /synthetic-check 返回 HTTP 500，错误码 TEST_FAILURE。", "positive"),
    ("合成对话：\n小林：接口尚未发布，计划周五验收。\n小周：收到，我只是转述。", "optional"),
    ("合成教程：\n示例输出为部署成功。\n这段文字仅为示例，不是真实部署记录。", "optional"),
    ("??? □□□ ……", "empty"),
])
async def test_selected_model_selects_only_original_blocks(runtime, source, expected):
    model = Ollama(runtime.settings.ollama, GenerationStore(runtime.settings.ollama).current)
    runtime.ollama = model
    await model.start()
    try:
        identifier, revision = await pending(runtime, source)
        await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
        async with runtime.sessions() as session:
            episode = await session.get(Episode, identifier)
            facts = (await session.scalars(select(WorkFact))).all()
            assert episode.evidence_state in {"excerpted", "no_evidence"}
            assert not await session.scalar(select(Project))
            if expected == "positive":
                assert facts and await valid_excerpt_sources(session, episode)
            elif expected == "empty":
                assert not facts and episode.summary is None
            for fact in facts:
                assert fact.text in source and fact.kind == "observation" and fact.state == "observed"
    finally:
        await model.close()
