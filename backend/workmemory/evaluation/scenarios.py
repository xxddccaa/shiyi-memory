"""Curated daily language. Editing this library changes its content fingerprint."""

from workmemory.evaluation.types import Check, Permit, Scenario, Stage


def check(table, key=None, *, count=1, contains=None, **values):
    return Check(table=table, where={"id": "$" + key} if key else {}, count=count,
                 values=values, contains={"text": contains} if contains else {})


def permit(action, target=None, *, words=(), bind=None, **values):
    if target:
        values["target_id"] = "$" + target
    if action == "todo.create":
        values = {"due_at": "$tomorrow_15", "timezone": "$timezone", "repeat": "once", "weekdays": [], "paused": False, "notify_pet": True, **values}
    return Permit(action=action, values=values, text_contains=list(words), bind=bind)


def stage(message, *, goal=None, permits=(), checks=(), **kwargs):
    return Stage(message=message, goal=goal or message, permits=list(permits), checks=list(checks), **kwargs)


def library():
    cases = []

    def add(identifier, group, openings, stages=None, *, seed=(), smoke=False, pipeline=False, pending=False):
        cases.append(Scenario(id=identifier, group=group, description=openings[0],
                              openings=list(openings), stages=stages or [stage(openings[0])],
                              seed=list(seed), smoke=smoke, pipeline=pipeline, pending=pending))

    def todo(identifier, openings, values, *, seed=("todos",), hook="none", decision="confirm", smoke=False):
        add(identifier, "待办", openings, [stage(openings[0],
            permits=[permit("todo.update", "todo_a", **values)],
            checks=[check("todos", "todo_a", **values)] if decision == "confirm" and hook not in {"expired", "changed"} else [],
            hook=hook, decision=decision)], seed=seed, smoke=smoke)

    add("todo-create", "待办", ("帮我新增一个待办：整理周报。", "记一下，整理周报这事还没做。", "周报还得整理，放进待办吧。"),
        [stage("新增待办整理周报。未给具体时间时等待助手询问，再回答明天下午三点提醒。", permits=[permit("todo.create", words=["周报"], bind="created")],
               checks=[check("todos", "created", contains=["周报"], confirmed=True, done=False)])], smoke=True)
    add("todo-create-due", "待办", ("新增待办交周报，截止明天下午三点。", "明儿15点前要交周报，记进待办。", "别忘了交周报，帮记一下，明天15:00截止。"),
        [stage("创建交周报待办，截止明天15:00", permits=[permit("todo.create", words=["周报"], due_at="$tomorrow_15", bind="created")],
               checks=[check("todos", "created", due_at="$tomorrow_15")])])
    add("todo-create-project", "待办", ("在蓝鲸项目下新增待办：补充验收测试。", "蓝鲸还差验收测试，帮我加个待办。", "给蓝鲸记件事：补充验收测试。"),
        [stage("为蓝鲸项目创建补充验收测试待办；时间未指定，等助手询问后补充明天下午三点。", permits=[permit("todo.create", words=["验收", "测试"], project_id="$blue", bind="created")],
               checks=[check("todos", "created", project_id="$blue", contains=["验收", "测试"])])])
    add("todo-list", "查询", ("我有什么待办记录？", "还有哪些事没做？", "把我没完成的待办列一下。"), seed=["todos"], smoke=True)
    add("todo-list-project", "查询", ("蓝鲸项目有哪些待办？", "只看蓝鲸的未完成任务。", "蓝鲸那边还有啥要做的？"), seed=["todos"])
    add("todo-list-completed", "查询", ("有哪些已经完成的待办？", "帮我翻翻勾掉的那些任务。", "查一下我的已完成事项。"), seed=["todos", "completed"])
    todo("todo-rename", ("把整理周报这个待办改名为整理月报。", "周报那条写错了，改成整理月报。", "待办里的整理周报，内容换成整理月报。"), {"text": "整理月报"})
    todo("todo-reschedule", ("把整理周报的截止时间改到明天下午三点。", "周报那条延期，明天15点截止。", "整理周报的时间改改，挪到明儿下午3点。"), {"due_at": "$tomorrow_15"})
    todo("todo-pause", ("暂停整理周报的提醒，保留事项。", "周报暂时别提醒了，但事还要做。", "把周报提醒暂停，别删任务。"), {"paused": True}, seed=["todos", "due"])
    todo("todo-complete", ("整理周报已经做好了，标记完成。", "周报搞定，帮我勾掉。", "周报交差了，把那条设为已完成。"), {"done": True}, smoke=True)
    todo("todo-reopen", ("整理周报还没做好，把它恢复未完成。", "周报刚才勾错了，取消完成。", "周报还得改，重新放回未完成列表。"), {"done": False}, seed=["todos", "completed"])
    todo("todo-dismiss", ("把整理周报这条待办设为忽略。", "周报这条先隐藏，设成已忽略。", "整理周报暂时不管，帮我忽略这条待办。"), {"dismissed": True})
    add("todo-delete-context", "待办", ("可以帮我删除这个待办记录吗？", "这条不要了，删掉吧。", "把当前选中的这个待办移除。"),
        [stage("删除当前选中的整理周报待办", context="todo_a", permits=[permit("todo.delete", "todo_a")],
               checks=[check("todos", "todo_a", count=0)])], seed=["todos"], smoke=True)
    add("todo-delete-named", "待办", ("删除整理周报这条待办，其他保留。", "整理周报不用做了，把那条删掉。", "只去掉整理周报那项。"),
        [stage("只删除整理周报", permits=[permit("todo.delete", "todo_a")], checks=[check("todos", "todo_a", count=0)])], seed=["todos"])
    add("todo-delete-reject", "确认", ("我想删掉整理周报，先给我看看。", "整理周报那条准备删除，先别执行。", "给我看一下删除周报待办的确认卡片。"),
        [stage("先提出删除整理周报的操作；看到卡片后反悔，拒绝删除。", permits=[permit("todo.delete", "todo_a")], decision="reject",
               checks=[check("todos", "todo_a", text="整理周报")])], seed=["todos"], smoke=True)
    for identifier, seed, count, openings in [
        ("todo-bulk-69", ["bulk69"], 69, ("请把我的所有待办全部删除。", "待办清空，一个都不留。", "那些待办都不需要了，全部删掉。")),
        ("todo-bulk-105", ["bulk105"], 105, ("删除所有待办，超过一页也要删完整。", "整个待办列表清空。", "把全部待办一次性移除。")),
    ]:
        add(identifier, "批量", openings, [stage(f"删除全部{count}条待办，确认完整清单后执行", permits=[permit("todo.delete_many", target_ids="$bulk_ids")],
            checks=[check("todos", count=0)])], seed=seed, smoke=count == 69)
    add("todo-bulk-project", "批量", ("只删除蓝鲸项目的所有待办。", "蓝鲸的待办清掉，星河别动。", "把蓝鲸下面的待办全移除。"),
        [stage("删除蓝鲸待办，保留星河任务", permits=[permit("todo.delete_many", target_ids="$blue_todo_ids")],
               checks=[check("todos", "todo_a", count=0), check("todos", "todo_b", count=0), check("todos", "todo_guard")])], seed=["todos"])
    add("todo-bulk-completed", "批量", ("删除所有已完成的待办。", "勾掉的任务清理掉，没做的留着。", "已完成列表清空。"),
        [stage("只删除已完成的整理周报", permits=[permit("todo.delete_many", target_ids=["$todo_a"])], checks=[check("todos", "todo_a", count=0)])], seed=["todos", "completed"])
    add("todo-merge", "待办", ("把整理周报和整理本周周报合并，保留整理周报。", "这两条周报任务重复，合成一条，保留整理周报。", "整理本周周报并到整理周报那条里。"),
        [stage("合并周报重复项，保留整理周报", permits=[permit("todo.merge", "todo_a", other_id="$todo_b")],
               checks=[check("todos", "todo_a", dismissed=False), check("todos", "todo_b", dismissed=True)])], seed=["todos", "duplicate"])
    add("todo-ambiguous", "连续对话", ("把整理周报删掉。", "周报那条不要了。", "帮我去掉周报那个待办。"),
        [stage("有两条同名整理周报。先让助手澄清；被问到时说明只删除蓝鲸那条，保留星河那条。", permits=[permit("todo.delete", "todo_a")],
               checks=[check("todos", "todo_a", count=0), check("todos", "todo_guard", text="整理周报")])], seed=["todos", "ambiguous"])
    add("todo-missing", "边界", ("删除一个叫火星种植的待办。", "把火星种植那项去掉。", "火星种植不做了，删掉它。"), seed=["todos"])
    add("todo-chain", "连续对话", ("帮我加一个待办：交周报。", "周报要交，记到待办里。", "记件事，交周报。"), [
        stage("创建交周报待办，等助手询问时间后补充明天下午三点。", permits=[permit("todo.create", words=["周报"], bind="created")], checks=[check("todos", "created", contains=["周报"])]),
        stage("刚才那个，截止时间改到明天下午三点。", permits=[permit("todo.update", "created", due_at="$tomorrow_15")], checks=[check("todos", "created", due_at="$tomorrow_15")]),
        stage("我现在有什么待办？"),
        stage("刚才那个周报待办不要了，删除吧。", permits=[permit("todo.delete", "created")], checks=[check("todos", "created", count=0)]),
        stage("再查一下，周报待办还在吗？", checks=[check("todos", "created", count=0)]),
    ], smoke=True)
    todo("confirm-duplicate", ("整理周报做好了，标记完成。", "把周报勾选为完成。", "周报完事了，更新一下状态。"), {"done": True}, hook="duplicate")
    todo("confirm-expired", ("把整理周报标记完成。", "周报完成了，先给我确认。", "周报做完，生成完成卡片。"), {"done": True}, hook="expired")
    todo("confirm-changed", ("把整理周报标记完成。", "周报弄好了，帮我改状态。", "我做好周报了，勾选一下。"), {"done": True}, hook="changed")
    add("bulk-new-target", "确认", ("删除我的全部待办。", "现在这些待办都清掉。", "帮我清空目前的任务。"),
        [stage("删除确认卡片中列出的69条，后来新增的任务保留", permits=[permit("todo.delete_many", target_ids="$bulk_ids")], hook="new_todo",
               checks=[check("todos", "late_todo", text="后来新增的合成任务"), Check(table="todos", count=1)])], seed=["bulk69"])
    add("request-idempotent", "确认", ("新建待办：检查日志。", "记个检查日志的任务。", "检查日志还没做，加入待办。"),
        [stage("创建一条检查日志待办，未给时间时等询问再补充明天下午三点。发送请求重试不能重复创建", hook="resend", permits=[permit("todo.create", words=["日志"], bind="created")],
               checks=[Check(table="todos", count=1, contains={"text": ["日志"]})])])
    add("run-cancel", "确认", ("帮我查查今天的工作。", "总结一下今日工作记录。", "今天干了什么，整理给我。"),
        [stage("发送后立即取消，确认运行停止且业务数据不变", hook="cancel")], seed=["work"])
    add("run-retry", "确认", ("我有什么待办？", "查一下任务清单。", "看看还有什么没做。"),
        [stage("遇到一次连接失败后重新回答原来的待办查询", hook="retry")], seed=["todos"])
    add("memory-create", "记忆", ("请记住：我喜欢用中文交流。", "以后跟我用中文说话，帮我记住这个偏好。", "记一下，我偏好中文交流。"),
        [stage("记住偏好中文交流", permits=[permit("memory.create", words=["中文"], bind="created")],
               checks=[check("memory_candidates", "created", status="confirmed", contains=["中文"])])], smoke=True)
    add("memory-query", "记忆", ("我喜欢喝什么咖啡？", "我平时的咖啡口味是什么？", "还记得我爱喝哪种咖啡吗？"), seed=["memory"])
    add("memory-update", "记忆", ("我现在喜欢拿铁了，把咖啡偏好从美式改成拿铁。", "咖啡偏好变了，别记美式了，改记拿铁。", "之前记的美式不对，我喜欢拿铁，请更正。"),
        [stage("将现有咖啡偏好更正为拿铁，不新增冲突记忆", permits=[permit("memory.update", "memory_a", words=["拿铁"])],
               checks=[check("memory_candidates", "memory_a", status="confirmed", contains=["拿铁"])]),
         stage("那我现在喜欢什么咖啡？", forbidden_answer=["喜欢美式"])], seed=["memory"])
    add("memory-forget", "记忆", ("忘记我的咖啡偏好。", "把我爱喝美式那条记忆删除。", "不想让你记我的咖啡口味了。"),
        [stage("忘记咖啡偏好", permits=[permit("memory.forget", "memory_a")], checks=[check("memory_candidates", "memory_a", text=None, status="dismissed")]),
         stage("我喜欢喝什么咖啡？", before="new_conversation", forbidden_answer=["喜欢美式"])], seed=["memory"], smoke=True)
    add("memory-reject", "记忆", ("帮我记住我喜欢拿铁，先给我确认。", "我爱喝拿铁，准备记下来让我看看。", "拿铁是我的偏好，先出个记忆确认。"),
        [stage("提出记录拿铁偏好后反悔，拒绝保存", decision="reject", permits=[permit("memory.create", words=["拿铁"])], checks=[Check(table="memory_candidates", where={"status": "confirmed"}, count=0)])])
    add("work-today", "工作记录", ("你知道我今天在做什么工作吗？", "今天我忙了些什么？", "说说我今日在干啥。"),
        [stage("依据今天记录说明蓝鲸接口验收通过，以及仍需补充测试。", required_tools=["get_work_day"])], seed=["work"], smoke=True)
    add("work-summary", "工作记录", ("总结我今日份工作内容。", "帮我整理今天的工作小结。", "今日工作回顾给我来一份。"), seed=["work"], pipeline=True, smoke=True)
    add("work-yesterday", "工作记录", ("昨天我做了什么？", "回顾一下昨日工作。", "翻翻昨天忙的事情。"), seed=["yesterday"])
    add("work-empty", "边界", ("总结我今天的工作。", "今天我忙了些什么？", "根据记录讲讲今天的工作内容。"))
    add("work-pending", "边界", ("今天的工作记录整理好了吗？", "今天我干了什么，记录还在处理中也告诉我。", "查查今天的工作，没整理完就说明。"), seed=["work"], pipeline=True, pending=True)
    add("episode-correct", "工作记录", ("把蓝鲸接口验收通过的记录更正为蓝鲸接口验收未通过，需要修复超时。", "蓝鲸验收结果记反了，实际未通过，需要修复超时，请纠正。", "修正蓝鲸那条记录：接口验收未通过，需要修复超时。"),
        [stage("纠正蓝鲸工作记录为验收未通过，需要修复超时", permits=[permit("episode.correct", "episode_a", words=["未通过", "超时"])],
               checks=[Check(table="episodes", where={"id": "$episode_a"}, values={"corrected": True}, contains={"summary": ["未通过", "超时"]})]),
         stage("蓝鲸接口验收到底过了吗？", forbidden_answer=["验收已通过"])], seed=["work"])
    add("episode-delete", "工作记录", ("删除蓝鲸接口验收那条工作记录。", "蓝鲸验收这个片段不要了，删除。", "把蓝鲸接口验收那段记录移除。"),
        [stage("删除蓝鲸接口验收片段", permits=[permit("episode.delete", "episode_a")], checks=[check("episodes", "episode_a", summary=None, status="deleted")]),
         stage("你还有蓝鲸接口验收通过的依据吗？", before="new_conversation", forbidden_answer=["验收已通过"])], seed=["work"])
    add("episode-move", "工作记录", ("把蓝鲸接口验收这条记录移到星河项目。", "接口验收归错项目了，从蓝鲸挪到星河。", "那段验收工作其实属于星河，调整归属。"),
        [stage("移动蓝鲸接口验收记录到星河", permits=[permit("episode.move", "episode_a", project_id="$star")], checks=[check("episodes", "episode_a", project_id="$star")])], seed=["work"])
    add("session-update", "工作记录", ("把今天蓝鲸的工作段改名为接口排查，分类改成调试。", "蓝鲸这段工作标题写接口排查，归到调试。", "今日蓝鲸工作段调整一下：接口排查，调试类。"),
        [stage("将蓝鲸工作段标题改成接口排查，类别调试", permits=[permit("session.update", "session_a", text="接口排查", category="调试")],
               checks=[check("work_sessions", "session_a", title="接口排查", category="调试")])], seed=["work"])
    add("project-delete", "项目", ("删除蓝鲸项目，保留工作记录和待办。", "蓝鲸这个项目分类不要了，里面的记录留着。", "移除蓝鲸项目，其他项目不动。"),
        [stage("删除蓝鲸项目分类，保留其待办为未归属", permits=[permit("project.delete", "blue")], checks=[check("projects", "blue", state="deleted"), check("projects", "star", state="active"), check("todos", "todo_a", project_id=None, text="整理周报")])], seed=["todos"])
    add("project-delete-all-32", "批量", ("删除所有项目，一次确认就行。", "把项目空间的项目全部清掉。", "全部项目都移除，别让我逐个点确认。"),
        [stage("生成一张含全部32个项目的卡片，一次确认删除分类，保留原始记录和待办", permits=[permit("project.delete_many", target_ids="$project_ids")], checks=[Check(table="projects", where={"state": "deleted"}, count=32), check("todos", "todo_a", project_id=None, text="整理周报")])], seed=["projects32", "todos"])
    add("project-query", "项目", ("蓝鲸项目目前进展怎样？", "蓝鲸推进到哪一步了？", "给我看看蓝鲸项目概况。"), seed=["work", "todos"])
    add("project-alias", "项目", ("给蓝鲸项目加一个别名鲸鱼。", "以后鲸鱼也指蓝鲸项目，添加这个别名。", "蓝鲸再加个名字叫鲸鱼。"),
        [stage("蓝鲸新增别名鲸鱼", permits=[permit("project.alias", "blue", text="鲸鱼")], checks=[Check(table="project_aliases", where={"name": "鲸鱼"}, values={"project_id": "$blue"})])])
    add("project-merge", "项目", ("把星河项目合并进蓝鲸，保留蓝鲸。", "星河和蓝鲸合成一个项目，以蓝鲸为主。", "星河那些工作归到蓝鲸，合并项目。"),
        [stage("星河合并进蓝鲸", permits=[permit("project.merge", "blue", other_id="$star")], checks=[check("projects", "star", state="merged"), check("todos", "todo_guard", project_id="$blue")])], seed=["todos"])
    add("review-save", "回顾", ("把今天的回顾正文改成：完成接口验收，明日补充测试。", "今日回顾我想写：完成接口验收，明日补充测试。请保存。", "编辑今天的回顾，用这段话：完成接口验收，明日补充测试。"),
        [stage("保存今天回顾的人工编辑正文", permits=[permit("review.save", "review_a", text="完成接口验收，明日补充测试。")], checks=[check("derived_artifacts", "review_a", user_text="完成接口验收，明日补充测试。")])], seed=["review"])
    add("chat-model", "闲聊", ("你是什么模型呀？", "你用的哪个模型？", "介绍一下你的模型名称。"), smoke=True)
    add("chat-greeting", "闲聊", ("你好呀！", "嗨，今天怎么样？", "哈喽，陪我聊两句。"))
    add("chat-knowledge", "闲聊", ("番茄工作法是什么？", "解释一下怎么用番茄钟安排工作。", "给我介绍下番茄工作法。"))
    add("chat-advice", "闲聊", ("今天有点累，给我一点安排工作的建议。", "事情太多没头绪，怎么安排比较好？", "我有点忙乱，帮我想想怎样分配精力。"))
    add("chat-mixed", "闲聊", ("我今天做了什么？再给点改善效率的建议。", "结合今日工作给我几个效率建议。", "回顾一下今天，再说说怎么做得更轻松。"), seed=["work"])
    add("unsupported", "边界", ("帮我直接给同事发邮件通知验收通过。", "替我发一封邮件给同事说验收好了。", "你现在打开邮箱把验收结果发出去。"))
    add("shared-pet", "共享会话", ("新增待办：准备演示。", "演示还没准备，记个待办。", "把准备演示加进任务里。"), [
        stage("新增准备演示待办，未给时间时等询问再回答明天下午三点。", permits=[permit("todo.create", words=["演示"], bind="created")], checks=[check("todos", "created", contains=["演示"])]),
        stage("刚才那个已经完成了，帮我标记。", before="pet", permits=[permit("todo.update", "created", done=True)], checks=[check("todos", "created", done=True)]),
    ])
    add("memory-compacted-forget", "来源生命周期", ("我喜欢喝什么咖啡？", "回忆一下我的咖啡偏好。", "我平时喝哪种咖啡？"), [
        stage("查询并回答已有咖啡偏好"),
        stage("忘记我的咖啡偏好。", before="compact", permits=[permit("memory.forget", "memory_a")], checks=[check("memory_candidates", "memory_a", status="dismissed", text=None)]),
        stage("现在还能知道我喜欢什么咖啡吗？", forbidden_answer=["喜欢美式"]),
        stage("我喜欢哪种咖啡？", before="new_conversation", forbidden_answer=["喜欢美式"]),
    ], seed=["memory"])
    add("todo-multi-intent", "连续对话", ("把整理周报标记完成，再新增一个待办准备演示。", "周报做完了勾掉，另记一件准备演示。", "两个事情：周报设完成，演示加待办。"),
        [stage("完成整理周报，并新增准备演示待办。演示时间未指定，等询问再补充明天下午三点。", permits=[permit("todo.update", "todo_a", done=True), permit("todo.create", words=["演示"], bind="created")],
               checks=[check("todos", "todo_a", done=True), check("todos", "created", contains=["演示"])])], seed=["todos"])
    add("todo-change-mind", "连续对话", ("我想删掉整理周报，先给我确认。", "准备移除周报待办，先出卡片。", "周报那条考虑删除，让我确认一下。"), [
        stage("原本想删除周报，看到卡片后反悔并拒绝", permits=[permit("todo.delete", "todo_a")], decision="reject", checks=[check("todos", "todo_a")]),
        stage("还是留着吧，只把它标记为完成。", permits=[permit("todo.update", "todo_a", done=True)], checks=[check("todos", "todo_a", done=True)]),
    ], seed=["todos"])
    add("reminder-daily", "定时提醒", ("每天15点提醒我写周报，从明天开始。", "设个每天的提醒，明天下午三点开始提醒写周报。", "帮我定每天写周报的提醒，首次明天15:00。"), [
        stage("从明天15:00开始每天提醒写周报", permits=[permit("todo.create", words=["周报"], repeat="daily", bind="created")],
              checks=[check("todos", "created", repeat="daily", due_at="$tomorrow_15", origin="manual")])])
    add("reminder-weekdays", "定时提醒", ("每周一到周五上午九点提醒我开晨会，从下周一开始。", "下周一开始，周一至周五9点提醒开晨会，不算调休。", "设个晨会提醒，下周一9点开始，每周工作日提醒，工作日按周一到周五。"), [
        stage("从下周一9:00开始周一至周五提醒开晨会，不含调休", permits=[permit("todo.create", words=["晨会"], due_at="$next_monday_09", repeat="weekdays", bind="created")],
              checks=[check("todos", "created", repeat="weekdays", due_at="$next_monday_09")])])
    add("reminder-weekly", "定时提醒", ("每周一、周三上午九点提醒我复盘，从下周一开始。", "下周一9点开始，设周一周三复盘的重复提醒。", "复盘要记住：每周一三9:00提醒，第一次是下周一。"), [
        stage("从下周一9:00开始，每周一和周三提醒复盘", permits=[permit("todo.create", words=["复盘"], due_at="$next_monday_09", repeat="weekly", weekdays=[1,3], bind="created")],
              checks=[check("todos", "created", repeat="weekly", weekdays=[1,3], due_at="$next_monday_09")])])
    add("reminder-no-time", "定时提醒", ("提醒我整理周报。", "帮我设个写周报的提醒。", "记个提醒，整理周报。"), [
        stage("仅说了事项，未授权任何时间；必须询问提醒日期时间，不能新建卡片或自行选择时间。助手询问后本样例结束，不补充时间。", allow_followups=False, decision="none", checks=[check("todos", count=0)])])
    add("reminder-observed-plan", "定时提醒", ("今天我计划写周报，先随便聊聊。", "周报明天要做，现在只是和你聊下计划。", "我还想整理一下周报，但暂时不用设提醒。"), [
        stage("闲聊工作计划，不是创建提醒指令；不要提出待办卡片。", decision="none", checks=[check("todos", count=0)])])
    add("reminder-complete-once", "定时提醒", ("这次周报提醒已经做完，完成本次，明天继续提醒。", "周报本次完成了，别取消每天的提醒。", "把今天这次整理周报提醒标成完成，保留重复计划。"), [
        stage("完成周报本次提醒，保持每日计划有效", permits=[permit("todo.complete_occurrence", "todo_a", reminder_id="$reminder_a", reminder_revision=1)],
              checks=[check("todos", "todo_a", done=False, repeat="daily"), check("reminders", "reminder_a", status="completed")])], seed=["todos", "due_instance"])
    add("reminder-snooze", "定时提醒", ("周报这个提醒十分钟后再提醒我。", "把刚到的整理周报提醒延后10分钟。", "周报这次提醒先缓一下，10分钟后再叫我。"), [
        stage("仅延后周报本次提醒10分钟，不更改每天计划", permits=[permit("todo.snooze", "todo_a", reminder_id="$reminder_a", reminder_revision=1)],
              checks=[check("todos", "todo_a", done=False, repeat="daily"), check("reminders", "reminder_a", status="snoozed", revision=2)])], seed=["todos", "due_instance"])
    todo("reminder-resume", ("恢复整理周报的提醒。", "周报这条重新启用提醒吧。", "把暂停的整理周报提醒开启。"), {"paused": False}, seed=["todos", "paused"])
    for c in cases:
        if any(p.action.startswith("todo.") for st in c.stages for p in st.permits):
            c.version = 2
    assert len({c.id for c in cases}) == len(cases)
    assert sum(c.smoke for c in cases) == 12
    return cases
