"""基于关键词的重复检测（纯规则，不依赖 AI/LLM）。

思路：把内容拆成关键词集合（英文单词去停用词 + 中文 2-gram），
跟最近已发内容算 Jaccard 相似度，超过阈值就判定为重复、跳过。
"""
from __future__ import annotations

import re

STOPWORDS = set(
    """a an the and or but if of to in on at by for with about as is are was were be
    been being this that these those it its i you he she we they them from up down
    out over under again further then once here there when where why how all any both
    each few more most other some such no nor not only own same so than too very can
    will just don should now has have had do does did did not who what which while
    after before because between during into through""".split()
)

_WORD_RE = re.compile(r"[a-z]{3,}")
_ZH_RE = re.compile(r"[\u4e00-\u9fff]+")


def extract_keywords(text: str) -> set:
    """提取关键词集合：英文单词（去停用词）+ 中文 2-gram。"""
    t = (text or "").lower()
    kw = [w for w in _WORD_RE.findall(t) if w not in STOPWORDS]
    for seg in _ZH_RE.findall(t):
        if len(seg) >= 2:
            kw.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return set(kw)


def jaccard(a, b) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_duplicate(text: str, recent: list[str], threshold: float = 0.6) -> bool:
    """判断 text 是否与 recent（最近已发内容）关键词高度重复。"""
    k1 = extract_keywords(text)
    if len(k1) < 3:
        return False  # 关键词太少，不判重，避免误杀短帖
    for rt in recent:
        k2 = extract_keywords(rt)
        if jaccard(k1, k2) >= threshold:
            return True
    return False
