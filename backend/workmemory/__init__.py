"""Project-local runtime defaults, including third-party tokenizer cache."""

from pathlib import Path

import jieba

_cache = Path(__file__).resolve().parents[2] / ".cache/jieba"
_cache.mkdir(parents=True, exist_ok=True)
jieba.dt.tmp_dir = str(_cache)
jieba.setLogLevel(40)
