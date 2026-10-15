import shutil
import uuid
from datetime import UTC, datetime

from conftest import FakeOllama
from workmemory.config import ROOT, get_settings
from workmemory.memory.base import MemoryDocument
from workmemory.memory.reme_adapter import ReMeAdapter


async def test_real_reme_write_index_restart_correct_delete():
    settings = get_settings().model_copy(deep=True)
    settings.memory.reme_workspace = ".cache/test-reme-" + uuid.uuid4().hex
    model = FakeOllama()
    episode_id = uuid.uuid4()
    adapter = ReMeAdapter(settings, model)
    try:
        await adapter.start()
        await adapter.upsert(
            MemoryDocument(episode_id, 1, "蓝鲸项目发布时间改到周一。", datetime.now(UTC), "蓝鲸")
        )
        assert episode_id in [hit.episode_id for hit in await adapter.search("蓝鲸发布时间")]
        assert model.embedding_calls > 0
        await adapter.close()
        adapter = ReMeAdapter(settings, model)
        await adapter.start()
        assert episode_id in [hit.episode_id for hit in await adapter.search("蓝鲸发布时间")]
        await adapter.correct(
            MemoryDocument(episode_id, 2, "蓝鲸实际发布时间改到周二。", datetime.now(UTC), "蓝鲸")
        )
        await adapter.delete(episode_id)
        assert await adapter.search("蓝鲸发布时间") == []
        await adapter.close()
        adapter = ReMeAdapter(settings, model)
        await adapter.start()
        assert await adapter.search("蓝鲸发布时间") == []
    finally:
        await adapter.close()
        shutil.rmtree(ROOT / settings.memory.reme_workspace)
