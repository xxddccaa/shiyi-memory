"""Translate Pi transcripts into the two existing generation wire protocols."""

import json
import uuid
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from workmemory.agent.errors import AgentFailure
from workmemory.inference.generation import chat_request

STRUCTURED_REPAIR = ContextVar("agent_structured_repair", default=False)
REPAIR_CITATIONS = ContextVar("agent_repair_citations", default=None)
FINAL_TOOL = "finish_answer"


class FinalAnswer(BaseModel):
    external_source_ids: list[str] = Field(default_factory=list)
    memory_status: Literal["not_needed", "supported", "insufficient"]
    answer: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    operation_status: Literal["none", "pending", "executed"] = Field(
        description="普通问答用none；请求用户确认业务操作或声称已生成卡片用pending，必须先有真实卡片；只有应用确认实际执行后才用executed。",
    )
    operation_ids: list[uuid.UUID] = Field(default_factory=list,
        description="操作状态对应的卡片ID，复制工具proposal.id或应用operation_id；不是业务object_id或证据ID。none时为空数组。",
    )

    @field_validator("answer")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Blank answer")
        return value


def text_content(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == "text")


def tool_request(config, transcript, tools, context_size):
    messages = []
    for message in transcript:
        role = message["role"]
        value = {"role": role, "content": text_content(message.get("content"))}
        if role == "toolResult":
            value["role"] = "tool"
            if config.provider == "ollama":
                value["tool_name"] = message["toolName"]
            else:
                value["tool_call_id"] = message["toolCallId"]
        elif role == "assistant":
            blocks = message.get("content", [])
            calls = [c for c in blocks if isinstance(c, dict) and c.get("type") == "toolCall"]
            if calls:
                value["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": c["arguments"]
                            if config.provider == "ollama"
                            else json.dumps(c["arguments"]),
                        },
                    }
                    for c in calls
                ]
        if role in {"system", "user", "assistant", "toolResult"}:
            messages.append(value)
    # The last request must return an answer, including an explicit insufficiency answer.
    # Use the already-tested structured output mode instead of relying on tool_choice alone.
    final_tool = config.provider == "openai_compatible" and bool(tools) and not any(t["name"] == "echo_probe" for t in tools)
    schema = FinalAnswer.model_json_schema() if final_tool or not tools or STRUCTURED_REPAIR.get() else None
    if schema:
        schema["required"] = list(dict.fromkeys([*schema.get("required", []), "operation_status", "operation_ids"]))
    known = REPAIR_CITATIONS.get()
    if schema and STRUCTURED_REPAIR.get() and known is not None:
        if known:
            schema["properties"]["evidence_ids"]["items"]["enum"] = list(known)
        else:
            schema["properties"]["evidence_ids"]["maxItems"] = 0
            schema["properties"]["memory_status"]["enum"] = ["not_needed", "insufficient"]
    path, body = chat_request(config, messages, None if final_tool else schema, config.max_tokens, context_size)
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
        if final_tool:
            body["tools"].append({"type": "function", "function": {
                "name": FINAL_TOOL,
                "description": "完成本轮回答时调用此工具提交最终答复。需要查资料或提出操作时先调用业务工具；不要输出普通文字或空answer占位。此工具只提交回答，不执行任何业务操作。",
                "parameters": schema,
            }})
        if config.provider == "openai_compatible":
            body["tool_choice"] = "required"
    elif config.provider == "openai_compatible":
        body["tool_choice"] = "none"
    return path, body


def tool_response(config, data):
    try:
        if config.provider == "ollama":
            message = data.get("message", {})
            reason = data.get("done_reason")
        else:
            choice = data.get("choices", [{}])[0]
            message, reason = choice.get("message", {}), choice.get("finish_reason")
        if reason == "length":
            raise AgentFailure("truncated_response", finish_reason=reason)
        if message.get("refusal") or reason == "content_filter":
            raise AgentFailure("model_refused", finish_reason=reason)
        if data.get("error"):
            raise AgentFailure("provider_response", finish_reason=reason)
        calls = []
        for call in message.get("tool_calls") or []:
            try:
                function = call["function"]
                arguments = function["arguments"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict) or not isinstance(function["name"], str):
                    raise ValueError()
                calls.append(
                    {
                        "id": call.get("id") or "call_" + uuid.uuid4().hex,
                        "name": function["name"],
                        "arguments": arguments,
                    }
                )
            except (KeyError, TypeError, ValueError):
                raise AgentFailure("tool_protocol", finish_reason=reason) from None
        if len(calls) > 64:
            raise AgentFailure("tool_protocol", finish_reason=reason)
        text = message.get("content") or ""
        finals = [call for call in calls if call["name"] == FINAL_TOOL]
        if config.provider == "openai_compatible" and finals:
            if len(calls) != 1:
                raise AgentFailure("tool_protocol", finish_reason=reason)
            # This transport-only submission becomes the SDK's ordinary final response.
            # It never enters MCP or runs a business operation.
            text, calls = json.dumps(finals[0]["arguments"], ensure_ascii=False), []
        if not isinstance(text, str):
            raise AgentFailure("provider_response", finish_reason=reason)
        if not (text.strip() or calls):
            raise AgentFailure("empty_response", finish_reason=reason, repairable=True)
        usage = data.get("usage") or {}
        prompt = data.get("prompt_eval_count", usage.get("prompt_tokens", 0)) or 0
        output = data.get("eval_count", usage.get("completion_tokens", 0)) or 0
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        return {"text": text, "tool_calls": calls, "usage": {
            "input": max(0, prompt - cached), "output": output, "cacheRead": cached,
            "cacheWrite": 0, "totalTokens": prompt + output,
        }, "usage_estimated": not ("usage" in data or "prompt_eval_count" in data)}
    except AgentFailure:
        raise
    except (KeyError, IndexError, TypeError, AttributeError):
        raise AgentFailure("provider_response") from None
