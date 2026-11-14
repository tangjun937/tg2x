"""DeepSeek 写作引擎：洗稿 + 互动形式 + 写作要求总册核心原则。

输入中/英文新闻原文，输出英文推文（X 用）。
写作规则提炼自「写作要求总册」的核心原则：只写数字和事实、中立不表态、
不编造、不解释、不重复、不加金句；互动形式三选一（简短判断/数据解读/提问）。
"""
from __future__ import annotations

import json
import logging
import re

import requests

from .config import Config

log = logging.getLogger("tg2x")

WRITE_SYSTEM = (
    "You are a neutral news writer posting to X (Twitter). Rewrite the given news item "
    "into one polished English post.\n\n"
    "WRITING RULES (strictly follow, adapted from a facts-only style guide):\n"
    "1. State only facts, numbers, dates, and events. No opinion, no stance, no praise or criticism.\n"
    "2. Stay neutral and non-partisan at all times. Do not take sides or editorialize.\n"
    "3. Never fabricate or exaggerate. Every number and name must match the source exactly.\n"
    "4. Do not explain or interpret — avoid 'this means', 'because', 'therefore', 'significantly', 'importantly'.\n"
    "5. Do not repeat the same point or reuse the same word twice.\n"
    "6. No clickbait, no golden-quote endings, no meta commentary about the writing itself.\n"
    "7. Keep people/place/company names and all figures exact.\n\n"
    "LENGTH: keep full detail — preserve key figures, context, and specifics. Do not over-truncate.\n\n"
    "INTERACTION: choose exactly ONE of the following, whichever fits the news best, and "
    "weave it naturally into the post:\n"
    "- 'judgment': a brief, factual, neutral takeaway.\n"
    "- 'data': a neutral reading of the key number or statistic.\n"
    "- 'question': a neutral question posed to the reader.\n\n"
    "Output ONLY the final post. No preamble, no labels, no surrounding quotes."
)

RANK_SYSTEM = (
    "You are a news editor. Rank the following news items by their potential global impact "
    "and newsworthiness, from highest to lowest.\n\n"
    "Return ONLY a JSON array of indices (0-based) in descending order of impact, like [2, 0, 1]. "
    "Do not include any other text."
)


class NewsWriter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.get("writer.enabled", True))
        self.base_url = (cfg.get("writer.base_url") or "https://api.deepseek.com/v1").rstrip("/")
        self.model = cfg.get("writer.model") or "deepseek-chat"
        self.key = cfg.env(cfg.get("writer.api_key_env", "DEEPSEEK_API_KEY"))
        self.temperature = float(cfg.get("writer.temperature", 0.7) or 0.7)
        self.timeout = int(cfg.get("writer.timeout", 60) or 60)
        self.session = requests.Session()

    def available(self) -> bool:
        return self.enabled and bool(self.key)

    def _chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        r = self.session.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:200]}")
        return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

    def write(self, text: str) -> str:
        """洗稿 + 互动形式 + 写作总册原则，输出英文推文。失败返回空串（调用方 fallback）。"""
        if not self.available() or not (text or "").strip():
            return ""
        try:
            return self._chat(WRITE_SYSTEM, (text or "").strip()).strip()
        except Exception as e:  # noqa: BLE001
            log.warning("DeepSeek 写作失败：%s", e)
            return ""

    def rank(self, posts) -> list:
        """按影响从大到小排序 posts，返回排序后的列表。失败返回原顺序。"""
        if len(posts) <= 1 or not self.available():
            return posts
        items = "\n".join(f"[{i}] {p.text[:220]}" for i, p in enumerate(posts))
        try:
            out = self._chat(RANK_SYSTEM, items)
            m = re.search(r"\[[\d,\s]+\]", out)
            if not m:
                return posts
            idx = json.loads(m.group(0))
            return [posts[i] for i in idx if isinstance(i, int) and 0 <= i < len(posts)]
        except Exception as e:  # noqa: BLE001
            log.warning("DeepSeek 影响排序失败：%s", e)
            return posts
