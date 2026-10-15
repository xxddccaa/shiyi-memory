import json

import pytest
from workmemory.agent.protocol import tool_request, tool_response
from workmemory.config import OllamaConfig
from workmemory.config.generation import local_generation


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_tool_protocol_preserves_call_identity_and_separates_credentials(provider):
    config = local_generation(OllamaConfig()).model_copy(update={"provider": provider})
    messages = [
        {"role": "system", "content": "synthetic"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call1",
                    "name": "search_memories",
                    "arguments": {"query": "验收"},
                }
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "call1",
            "toolName": "search_memories",
            "content": [{"type": "text", "text": "result"}],
        },
    ]
    path, body = tool_request(
        config, messages, [{"name": "search_memories", "parameters": {"type": "object"}}], 8192
    )
    assert body["stream"] is False and body["tools"][0]["type"] == "function"
    assert "api_key" not in json.dumps(body)
    if provider == "ollama":
        assert path == "/api/chat"
        assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == {"query": "验收"}
        assert body["messages"][2]["tool_name"] == "search_memories"
        data = {"message": {"content": "", "tool_calls": body["messages"][1]["tool_calls"]}}
    else:
        assert path == "/chat/completions"
        assert body["messages"][2]["tool_call_id"] == "call1"
        data = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {"content": None, "tool_calls": body["messages"][1]["tool_calls"]},
                }
            ]
        }
    result = tool_response(config, data)
    assert result["tool_calls"] == [
        {"id": "call1", "name": "search_memories", "arguments": {"query": "验收"}}
    ]


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_truncated_tool_response_is_not_published(provider):
    config = local_generation(OllamaConfig()).model_copy(update={"provider": provider})
    data = (
        {"message": {"content": "partial"}, "done_reason": "length"}
        if provider == "ollama"
        else {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
    )
    with pytest.raises(ValueError):
        tool_response(config, data)


def test_final_answer_requires_explicit_memory_basis():
    from pydantic import ValidationError
    from workmemory.agent.protocol import FinalAnswer

    for status in ("not_needed", "supported", "insufficient"):
        assert FinalAnswer(answer="测试回答", memory_status=status, operation_status="none").memory_status == status
    # Missing/unknown basis is repaired through the normal bounded protocol path,
    # never silently guessed from the wording of the user's question.
    for payload in ({"answer": "测试回答"}, {"answer": "测试回答", "operation_status": "none", "memory_status": "invented"}):
        with pytest.raises(ValidationError):
            FinalAnswer.model_validate(payload)


@pytest.mark.parametrize(
    "provider,structured_output",
    [
        ("ollama", "json_schema"),
        ("openai_compatible", "json_schema"),
        ("openai_compatible", "json_object"),
    ],
)
def test_final_round_requests_structured_answer_without_tools(provider, structured_output):
    config = local_generation(OllamaConfig()).model_copy(
        update={"provider": provider, "structured_output": structured_output}
    )
    _, body = tool_request(
        config,
        [
            {"role": "system", "content": "Only use supplied evidence"},
            {"role": "user", "content": "未知记录？"},
        ],
        [],
        8192,
    )
    assert "tools" not in body
    if provider == "ollama":
        assert body["format"]["properties"]["answer"]["minLength"] == 1
        assert body["format"]["properties"]["evidence_ids"]["type"] == "array"
        assert "memory_status" in body["format"]["required"]
    else:
        assert body["tool_choice"] == "none"
        assert body["response_format"]["type"] == structured_output
        if structured_output == "json_schema":
            assert (
                body["response_format"]["json_schema"]["schema"]["properties"]["answer"][
                    "minLength"
                ]
                == 1
            )
        else:
            assert "evidence_ids" in body["messages"][0]["content"]
    # Final formatting must preserve the question and source context.
    assert body["messages"][-1]["content"] == "未知记录？"


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize(
    "message,code,repairable",
    [
        ({"content": ""}, "empty_response", True),
        (
            {
                "content": "",
                "tool_calls": [{"function": {"name": "search_memories", "arguments": "{"}}],
            },
            "tool_protocol",
            False,
        ),
        ({"content": "", "refusal": "private provider explanation"}, "model_refused", False),
    ],
)
def test_safe_protocol_diagnostics_and_only_empty_output_is_repairable(
    provider, message, code, repairable
):
    from workmemory.agent.errors import AgentFailure

    config = local_generation(OllamaConfig()).model_copy(update={"provider": provider})
    data = (
        {"message": message}
        if provider == "ollama"
        else {"choices": [{"message": message, "finish_reason": "stop"}]}
    )
    with pytest.raises(AgentFailure) as exc:
        tool_response(config, data)
    assert exc.value.code == code and exc.value.repairable == repairable
    assert "private" not in json.dumps(exc.value.data())


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("tools", [[], [{"name": "list_objects", "parameters": {"type": "object"}}]])
def test_chat_uses_saved_large_output_and_context_without_hidden_caps(provider, tools):
    from workmemory.config.generation import GenerationConfig

    config = GenerationConfig(provider=provider, base_url="http://synthetic.test/v1",
                              model="synthetic", max_tokens=65536, context_window=1000000)
    _, body = tool_request(config, [{"role": "user", "content": "合成清单"}], tools, 8192)
    if provider == "ollama":
        assert body["options"]["num_predict"] == 65536
        assert body["options"]["num_ctx"] == 1000000
    else:
        assert body["max_tokens"] == 65536


def test_long_answer_and_many_citations_survive_protocol():
    from workmemory.agent.protocol import FinalAnswer

    config = local_generation(OllamaConfig()).model_copy(update={"provider": "openai_compatible"})
    text = json.dumps({"answer": "合成长清单。" * 5000, "operation_status": "none", "memory_status": "supported",
                       "evidence_ids": [f"todo:{i}" for i in range(79)]}, ensure_ascii=False)
    response = tool_response(config, {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
    result = FinalAnswer.model_validate_json(response["text"])
    assert len(result.answer) > 16000 and len(result.evidence_ids) == 79


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_schema_repair_preserves_tool_capability_and_saved_limits(provider):
    from workmemory.agent.protocol import STRUCTURED_REPAIR

    config = local_generation(OllamaConfig()).model_copy(update={"provider": provider})
    token = STRUCTURED_REPAIR.set(True)
    try:
        _, body = tool_request(config, [{"role": "user", "content": "合成查询"}],
                               [{"name": "list_objects", "parameters": {"type": "object"}}], 8192)
    finally:
        STRUCTURED_REPAIR.reset(token)
    assert body["tools"]
    if provider == "ollama":
        assert "format" in body
    else:
        assert body["tool_choice"] == "required" and "response_format" not in body
        assert body["tools"][-1]["function"]["name"] == "finish_answer"
        assert "answer" in body["tools"][-1]["function"]["parameters"]["required"]


def test_no_progress_guard_handles_cycles_but_allows_changed_results_and_paging():
    from workmemory.agent.output import stalled_tools

    first = ("list_objects", {"cursor": "a"}, {"items": [1]})
    second = ("get_object", {"identifier": "b"}, {"text": "same"})
    assert stalled_tools([first] * 3)
    assert stalled_tools([first, second] * 3)
    assert not stalled_tools([first, second] * 2)
    assert not stalled_tools([("list_objects", {"cursor": n}, {"items": [n]}) for n in range(80)])
    assert not stalled_tools([("get_work_day", {}, {"status": n}) for n in range(12)])


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("known", [(), ("memory:12345678-1234-1234-1234-123456789abc",)])
def test_repair_schema_only_allows_currently_supplied_personal_citations(provider, known):
    from workmemory.agent.protocol import REPAIR_CITATIONS, STRUCTURED_REPAIR
    config = local_generation(OllamaConfig()).model_copy(update={"provider": provider})
    repairing = STRUCTURED_REPAIR.set(True)
    sources = REPAIR_CITATIONS.set(known)
    try:
        _, body = tool_request(config, [{"role": "user", "content": "当前偏好？"}],
                               [{"name": "search_memories", "parameters": {"type": "object"}}], 8192)
    finally:
        STRUCTURED_REPAIR.reset(repairing)
        REPAIR_CITATIONS.reset(sources)
    schema = body["format"] if provider == "ollama" else body["tools"][-1]["function"]["parameters"]
    properties = schema["properties"]
    if known:
        assert properties["evidence_ids"]["items"]["enum"] == list(known)
    else:
        assert properties["evidence_ids"]["maxItems"] == 0
        assert "supported" not in properties["memory_status"]["enum"]
    assert body["tools"]  # The repair may retrieve new evidence before answering.


def test_finish_answer_is_a_transport_submission_not_a_business_tool():
    config = local_generation(OllamaConfig()).model_copy(update={"provider": "openai_compatible"})
    fields = {"answer": "已查询到合成记录。", "evidence_ids": [], "memory_status": "not_needed",
              "operation_status": "none", "operation_ids": []}
    final = {"id": "finish", "type": "function", "function": {
        "name": "finish_answer", "arguments": json.dumps(fields),
    }}
    value = tool_response(config, {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [final]}}]})
    assert value["tool_calls"] == [] and json.loads(value["text"]) == fields
    with pytest.raises(ValueError):
        tool_response(config, {"choices": [{"message": {"tool_calls": [final, {
            "id": "write", "function": {"name": "propose_operation", "arguments": "{}"},
        }]}}]})
