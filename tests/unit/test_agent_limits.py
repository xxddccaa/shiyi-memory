import json

import yaml
from workmemory.config import ROOT, AgentConfig, Settings


def test_existing_yaml_without_agent_section_gets_large_defaults_and_custom_values_survive():
    data = yaml.safe_load((ROOT / "config/app.example.yaml").read_text())
    assert AgentConfig.model_validate(data.pop("agent")) == AgentConfig()
    settings = Settings(
        **data, postgres_password="synthetic", api_token="synthetic", neo4j_password="synthetic"
    )
    assert settings.agent.max_model_calls == 1024
    assert settings.agent.max_tool_calls == 16384
    assert settings.agent.max_memory_searches == settings.agent.max_external_calls == 4096
    assert settings.agent.tool_result_chars == 16000000
    assert settings.agent.run_timeout_seconds == 86400
    settings = Settings(
        **data,
        agent={"max_model_calls": 2048},
        postgres_password="synthetic",
        api_token="synthetic",
        neo4j_password="synthetic",
    )
    assert settings.agent.max_model_calls == 2048 and settings.agent.max_tool_calls == 16384
    assert "synthetic" not in json.dumps(settings.agent.public())


def test_saved_model_window_overrides_legacy_character_target():
    from types import SimpleNamespace

    from workmemory.config.generation import GenerationConfig, agent_limits

    settings = SimpleNamespace(agent=AgentConfig())
    model = GenerationConfig(provider="openai_compatible", base_url="http://synthetic.test/v1",
                             model="synthetic", context_window=1000000, max_tokens=65536)
    assert agent_limits(settings, model).context_chars == 934464
    assert settings.agent.context_chars == 64000
    assert agent_limits(settings, model.model_copy(update={"context_window": None})).context_chars == 64000


def test_output_capacity_must_leave_room_for_input():
    import pytest
    from pydantic import ValidationError
    from workmemory.config.generation import GenerationConfig

    with pytest.raises(ValidationError):
        GenerationConfig(base_url="http://synthetic.test", model="synthetic",
                         context_window=8192, max_tokens=8192)
