"""Allowlisted diagnostics only: never persist a provider body, prompt or exception text."""

import httpx

from workmemory.inference.generation import ModelConfigurationChanged

MESSAGES = {
    "workspace_unavailable": "本机工作目录不存在或未绑定，请检查编程设置。",
    "execution_uncertain": "上次工具执行结果待核实，请检查文件或命令状态后继续，不能直接重放。",
    "invalid_context_capacity": "请在模型设置填写实际上下文容量，并为最大输出和压缩留出空间。",
    "context_overflow": "模型输入超出上下文容量，自动压缩未能恢复，请检查容量设置。",
    "compaction_failed": "上下文压缩未完成，原对话仍保留，请重试压缩或检查模型。",
    "empty_response": "模型返回了空回答，请重试。",
    "invalid_answer": "模型没有返回有效的回答格式，请重试。",
    "truncated_response": "模型服务在输出完成前停止了生成（长度上限）。请在模型设置中提高单次最大输出，并核对服务端的输出限制。",
    "tool_protocol": "模型的工具调用格式不正确，请重试或检查模型的工具支持。",
    "provider_response": "模型服务返回了无法识别的响应，请检查服务后重试。",
    "model_refused": "模型未接受这次请求，请调整问题后再试。",
    "model_auth": "模型服务认证失败，请在模型设置中检查密钥和访问权限。",
    "model_timeout": "等待模型回答超时，请重试。",
    "model_connection": "无法连接模型服务，请检查服务地址和网络后重试。",
    "model_http": "模型服务暂时无法完成请求，请检查服务状态后重试。",
    "model_changed": "生成模型已更新，请重试。",
    "tool_failed": "读取记忆或创建操作提案失败，请刷新后重试。",
    "budget_exhausted": "本轮达到应用配置的执行上限。可以提高 agent 配置中的调用上限后重试。",
    "source_invalidated": "相关记忆已更新或到期，请重新提问。",
    "invalid_citation": "回答包含无法核验的引用，请重试。",
    "interrupted": "回答已中断，请重试。",
    "cancelled": "已取消回答。",
    "agent_unavailable": "内置 Agent 暂不可用，请重试或运行安装／修复。",
    "tool_unsupported": "当前模型未通过工具调用测试，请在模型设置中检查工具支持。",
    "unknown": "回答未完成，未记录到明确原因。请重试。",
}


class AgentFailure(ValueError):
    def __init__(self, code, *, stage="model", status=None, finish_reason=None, repairable=False):
        self.code = code if code in MESSAGES else "unknown"
        self.stage = (
            stage
            if stage in {"model", "repair", "tool", "probe", "validation", "agent", "compaction"}
            else "agent"
        )
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        self.finish_reason = (
            finish_reason
            if finish_reason
            in {"stop", "length", "tool_calls", "content_filter", "error", "load", "unload"}
            else None
        )
        self.repairable = repairable
        super().__init__(MESSAGES[self.code])

    def data(self):
        return {
            k: v
            for k, v in {
                "error_code": self.code,
                "stage": self.stage,
                "http_status": self.status,
                "finish_reason": self.finish_reason,
            }.items()
            if v is not None
        }

    @classmethod
    def from_data(cls, data):
        data = data if isinstance(data, dict) else {}
        return cls(
            data.get("error_code"),
            stage=data.get("stage"),
            status=data.get("http_status"),
            finish_reason=data.get("finish_reason"),
        )


def diagnose(exc, stage="model"):
    if isinstance(exc, AgentFailure):
        return exc
    if isinstance(exc, ModelConfigurationChanged):
        return AgentFailure("model_changed", stage=stage)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return AgentFailure("model_timeout", stage=stage)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # Inspect only the provider's error classification, never persist its body.
        if status in {400, 413, 422}:
            try:
                body = exc.response.json()
                error = body.get("error", {})
                code = error.get("code", "") if isinstance(error, dict) else ""
                message = error.get("message", "") if isinstance(error, dict) else str(error)
                if code in {"context_length_exceeded", "context_window_exceeded"} or any(term in message.lower() for term in ("maximum context length", "context window", "too many input tokens")):
                    return AgentFailure("context_overflow", stage=stage)
            except (ValueError, AttributeError):
                pass
        return AgentFailure(
            "model_auth" if status in {401, 403} else "model_http", stage=stage, status=status
        )
    if isinstance(exc, httpx.RequestError):
        return AgentFailure("model_connection", stage=stage)
    return AgentFailure("tool_failed" if stage == "tool" else "provider_response", stage=stage)
