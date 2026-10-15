"""Bounded suggestions only. Similarity never authorizes a business mutation."""

import re
from difflib import SequenceMatcher

from sqlalchemy import select

from workmemory.db.models import Episode, Todo, TodoMergeRejection


def similarity(left, right):
    left, right = (
        re.sub(r"[\s，。！？、,.!?：:；;]+", "", value.casefold()) for value in (left, right)
    )
    a = {left[i : i + 2] for i in range(len(left) - 1)}
    b = {right[i : i + 2] for i in range(len(right) - 1)}
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    return max(SequenceMatcher(None, left, right, autojunk=False).ratio(), overlap)


def memory_conflicts(candidate, confirmed):
    if candidate.status != "pending" or not candidate.text:
        return []
    matches = [
        (similarity(candidate.text, c.text), c)
        for c in confirmed
        if c.text and c.id != candidate.id
    ]
    return [
        {
            "id": str(c.id),
            "text": c.text,
            "revision": c.revision,
            "notice": "内容可能相关或冲突，请核对；相似度不代表事实冲突。",
        }
        for score, c in sorted(matches, key=lambda pair: (-pair[0], str(pair[1].id)))[:3]
        if score >= 0.42
    ]


async def todo_suggestions(session):
    rows = (
        await session.execute(
            select(Todo, Episode.project_id)
            .outerjoin(Episode, Todo.episode_id == Episode.id)
            .where(
                Todo.dismissed.is_(False),
                Todo.done.is_(False),
                (Todo.episode_id.is_(None)) | (Episode.deleted_at.is_(None)),
            )
            .order_by(Todo.id)
            .limit(200)
        )
    ).all()
    rejected = {
        (r.left_id, r.right_id) for r in (await session.scalars(select(TodoMergeRejection))).all()
    }
    result = []
    for index, (left, lp) in enumerate(rows):
        for right, rp in rows[index + 1 :]:
            if (
                (left.id, right.id) in rejected
                or (left.project_id or lp) != (right.project_id or rp)
                or any(getattr(left, field) != getattr(right, field)
                       for field in ("due_at", "repeat", "timezone", "weekdays", "paused"))
            ):
                continue
            # Different numbers/dates are not equivalent; the user can still manage them separately.
            if re.findall(r"\d+", left.text) != re.findall(r"\d+", right.text):
                continue
            score = similarity(left.text, right.text)
            if score >= 0.6:
                result.append(
                    {
                        "left": {"id": str(left.id), "text": left.text, "revision": left.revision},
                        "right": {
                            "id": str(right.id),
                            "text": right.text,
                            "revision": right.revision,
                        },
                        "score": round(score, 3),
                    }
                )
    return {"items": sorted(result, key=lambda v: -v["score"])[:20], "limited": len(rows) == 200}
