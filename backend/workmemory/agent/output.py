"""Fit complete JSON records to a shared turn budget, always mark omitted coverage."""
import copy
import json


def bounded_result(value, budget):
    result = copy.deepcopy(value)
    limit = max(0, budget)
    def size():
        return len(json.dumps(result, ensure_ascii=False))
    if size() <= limit:
        return result
    result["truncated"] = True
    result["notice"] = "资料因本轮预算截取，不能声称覆盖全部。可分页继续，完整日报请安排整理。"
    # Trim text values but preserve identifiers and metadata required for citations/paging.
    def trim(node, chars):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in {"text", "content", "name"} and isinstance(val, str) and len(val) > chars:
                    node[key] = val[:chars] + "…"
                elif isinstance(val, (dict, list)):
                    trim(val, chars)
        elif isinstance(node, list):
            for val in node:
                trim(val, chars)
    for chars in [900, 450, 180]:
        trim(result, chars)
        if size() <= limit:
            return result
    # A truncated report must never look like a complete daily report.
    if result.get("report"):
        result["report"] = None
        result["report_omitted"] = True
    for key in ["items", "projects", "todos", "memories", "external_sources"]:
        while result.get(key) and size() > limit:
            result[key].pop()
    if size() <= limit:
        return result
    return {"error": {"code": "context_budget", "message": "资料超过本轮预算，请缩小范围；不能声称已读取全部。"}}


def stalled_tools(calls):
    """Only stop stable repeated results; changed targets/cursors/results remain progress."""
    signatures = [json.dumps(call, sort_keys=True, ensure_ascii=False, default=str) for call in calls]
    for width in range(1, 5):
        size = width * 3
        if len(signatures) >= size:
            tail = signatures[-size:]
            if tail == tail[:width] * 3:
                return True
    return False
