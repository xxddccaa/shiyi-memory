"""Deterministic OCR filtering. Patches reference a full baseline, never another patch."""

import difflib
import re
import unicodedata

from rapidfuzz.distance import Levenshtein

DEFAULT_IMPORTANT = [
    r"\d+(?:[.,:/%-]\d+)*",
    r"[零一二三四五六七八九十百千万亿]+(?:元|日|月|年|天|点|时|分)",
    r"(?:周|星期)[一二三四五六日天]",
    r"今天|明天|后天|昨天|下周|本周",
    r"未完成|已完成|进行中|待处理|失败|成功|取消|通过|拒绝|禁止|允许|错误|异常|超时|不|未|无|否|已",
    r"\b(?:not|no|never|done|pending|completed|failed|success|error|warning|timeout|false|true)\b",
]


def normalize(text):
    return re.sub(
        r"[ \t]+", " ", unicodedata.normalize("NFC", text.replace("\x00", "").replace("\r\n", "\n"))
    ).strip()


def similarity(before, after):
    return Levenshtein.normalized_similarity(before, after)


def patch(before, after):
    # One replacement span is linear to construct and exactly reversible, even with repeated lines.
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    tail = 0
    while tail < min(len(before), len(after)) - start and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    return {
        "start": start,
        "end": len(before) - tail,
        "text": after[start : len(after) - tail if tail else len(after)],
    }


def restore(before, delta):
    if not delta:
        return before
    return before[: delta["start"]] + delta["text"] + before[delta["end"] :]


def significant(before, after, threshold=0.95, patterns=None):
    if before == after:
        return False, 1.0, "identical"
    score = similarity(before, after)
    if score < threshold:
        return True, score, "changed"
    for pattern in patterns if patterns is not None else DEFAULT_IMPORTANT:
        if re.findall(pattern, before, re.IGNORECASE) != re.findall(pattern, after, re.IGNORECASE):
            return True, score, "important"
    # A new/deleted sentence or whole nonempty line is not safely explained by OCR noise.
    old = re.split(r"[\n。！？!?;；]", before)
    new = re.split(r"[\n。！？!?;；]", after)
    for tag, i, j, a, b in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in {"insert", "delete"} and any(s.strip() for s in old[i:j] + new[a:b]):
            return True, score, "sentence"
    return False, score, "near_duplicate"
