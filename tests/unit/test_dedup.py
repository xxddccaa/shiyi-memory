
import pytest
from workmemory.ingestion.dedup import patch, restore, significant


@pytest.mark.parametrize(
    "old,new",
    [
        ("", "文字"),
        ("abc", ""),
        ("重复重复文本", "重复新增重复文本"),
        ("金额100元", "金额1000元"),
        ("same", "same"),
    ],
)
def test_reversible_span(old, new):
    assert restore(old, patch(old, new)) == new


@pytest.mark.parametrize(
    "old,new",
    [
        ("周五", "周一"),
        ("100", "1000"),
        ("未完成", "已完成"),
        ("success", "error"),
        ("允许", "不允许"),
    ],
)
def test_protect_small_facts(old, new):
    prefix = "无变化的页面正文。" * 150
    keep, score, reason = significant(prefix + old, prefix + new)
    assert score >= 0.95 and keep and reason == "important"


def test_noise_and_new_sentence():
    baseline = "页面正文内容。" * 150 + "末尾字符"
    assert not significant(baseline, baseline[:-1] + "符")[0]
    assert significant(baseline, baseline + "\n增加验收要求。")[0]
