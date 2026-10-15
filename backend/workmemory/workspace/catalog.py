"""Shared business tool contracts; neither Pi nor MCP owns these schemas."""

from workmemory.workspace.merge import MergeInput
from workmemory.workspace.operations import ProposalInput


def schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


STRING = {"type": "string", "minLength": 1, "maxLength": 2000}
TOOLS = [
    {
        "name": "search_memories",
        "description": "仅在需要回忆用户此前的个人历史事实时检索工作记忆和确认记忆。不要为闲聊、当前情绪、通用知识或学习建议调用；用户当前提供的信息无需再查。可改写问题再查，遵循应用提供的本轮检索上限。",
        "parameters": schema(
            {
                "query": STRING,
                "project_id": {"type": "string", "format": "uuid"},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "app": {"type": "string"},
            },
            ["query"],
        ),
    },
    {
        "name": "get_evidence",
        "description": "读取指定证据片段的当前有效内容，失效时不返回旧内容。",
        "parameters": schema(
            {"ids": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 8}}, ["ids"]
        ),
    },
    {
        "name": "get_neighbors",
        "description": "查看一条工作记录前后的同项目记录，最多各一条。",
        "parameters": schema({"episode_id": {"type": "string", "format": "uuid"}}, ["episode_id"]),
    },
    {
        "name": "get_project_brief",
        "description": "读取项目的当前证据概要，不创建或修改项目。",
        "parameters": schema({"project_id": {"type": "string", "format": "uuid"}}, ["project_id"]),
    },
]

TOOLS.extend(
    [
        {
            "name": "list_workspace",
            "description": "列出可操作的项目、待办和确认记忆及其ID；用于用户询问或管理这些对象；evidence_id 可用于回答引用。",
            "parameters": schema({}),
        },
        {
            "name": "propose_operation",
            "description": "仅在用户明确要求新增/修改/删除等数据变更，且目标明确时生成应用内修改提案。只查询、回顾或总结时不使用；检索结果中的计划不是创建待办的授权，返回待确认卡片；不执行修改。同名目标未确定时，直接最终回答询问并结束本轮，不能生成多张卡片供选择。合并现有条目用todo.merge或project.merge：target_id保留，other_id被并入；不是重写正文。批量清空待办用 todo.delete_many + all_todos=true，服务自动固定完整清单，不用逐条读取/提案。指定范围可用 project_id、done 或 target_ids。批量清空项目用 project.delete_many + all_projects=true，一张卡片一次确认；指定多个项目用 target_ids，单个用 project.delete + target_id。删除项目分类及别名，原始记录、待办提醒和确认记忆保留为未归属，不删除本机文件。完成本次todo.complete_occurrence或延后10分钟todo.snooze仅传action、target_id、reminder_id、reminder_revision；不可传due_at/timezone/time_expression/done，延后时间由确认服务计算。必须等待用户在界面确认。",
            "parameters": ProposalInput.model_json_schema(),
        },
    ]
)

TOOLS.extend(
    [
        {
            "name": "list_objects",
            "description": "搜索并分页查找待办todo、项目project、确认记忆memory、片段episode、工作段session、人工回顾review。工作段改名/分类用session，回顾保存用review，不能用episode替代。day按本地日期过滤；query只填标题关键词，不填内部ID。返回原始text、项目名称及准确ID/版本。",
            "parameters": schema(
                {
                    "kind": {"type": "string", "enum": ["todo", "project", "memory", "episode", "session", "review"]},
                    "query": {"type": "string", "maxLength": 300},
                    "project_id": {"type": "string", "format": "uuid"},
                    "done": {"type": "boolean"},
                    "day": {"type": "string", "format": "date"},
                    "cursor": {"type": "string", "format": "uuid"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["kind"],
            ),
        },
        {
            "name": "get_object",
            "description": "读取指定业务对象并核对版本，来源不存在或失效时不会返回旧正文。",
            "parameters": schema(
                {
                    "kind": {"type": "string", "enum": ["todo", "project", "memory", "episode", "session", "review"]},
                    "identifier": {"type": "string", "format": "uuid"},
                    "revision": {"type": "integer", "minimum": 0},
                },
                ["kind", "identifier"],
            ),
        },
        {
            "name": "get_work_day",
            "description": "用户问今天/今日份/某日做了什么或总结工作时首先使用本工具，按本地日期读取记录而非关键词匹配。返回采集状态、未整理数量、分页证据和已有整日报告。prepare_report 可排队整理报告；不要把未整理误说成无记录。",
            "parameters": schema(
                {
                    "day": {"type": "string", "format": "date"},
                    "cursor": {"type": "string", "format": "uuid"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                    "prepare_report": {"type": "boolean"},
                }
            ),
        },
    ]
)


TOOLS.append({
    "name": "propose_merge",
    "description": "把一条已有待办并到另一条、合并重复任务，或把项目A并入项目B时，优先使用此工具。分别传被并入source_name和保留target_name；服务精确查找现有对象，生成一张合并确认卡片。同名或缺失时返回匹配信息，不能猜。名称里有整理/写等动词也按完整标题理解，合并不是改标题、写周报或创建新任务。",
    "parameters": MergeInput.model_json_schema(),
})
