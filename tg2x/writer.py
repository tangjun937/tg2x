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
    "into one polished English post that reads like a person wrote it, not an AI.\n\n"
    "FACTS ONLY (from the style guide):\n"
    "1. State only facts, numbers, dates, and events. No opinion, no stance, no praise or criticism.\n"
    "2. Stay neutral and non-partisan. Do not take sides or editorialize.\n"
    "3. Never fabricate or exaggerate. Every number and name must match the source exactly.\n"
    "4. Do not explain or interpret — avoid 'this means', 'because', 'therefore'.\n"
    "5. Do not repeat the same point or reuse the same word twice.\n"
    "6. Keep people/place/company names and all figures exact.\n\n"
    "NO AI-SOUNDING WRITING (remove these patterns on sight):\n"
    "- No 'not X but Y' contrasts, no 'it's not X, it's Y'. State the point directly.\n"
    "- No one-line closers or dramatic fragments ('Let that sink in', 'That is the real win').\n"
    "- No fake-deep sayings ('at its core', 'what really matters', 'the language of trust').\n"
    "- No staged run-ups ('Here's the thing', 'Let's dive in', 'Honestly?').\n"
    "- No arguing with no one ('I'm not saying', 'To be clear', 'Some might say... but').\n"
    "- No forced triads — use only as many items as the meaning needs.\n"
    "- No em dashes or en dashes; use periods, commas, or colons.\n"
    "- No stacked qualifiers ('could potentially possibly').\n"
    "- No overused AI words: delve, pivotal, landscape, showcase, testament, underscore, robust, crucial, tapestry, realm, intricate, meticulous, vibrant, bolster.\n"
    "- No inflated significance ('marking a pivotal moment', 'the future looks bright', 'a step in the right direction').\n"
    "- No vague connections ('associated with', 'in connection with'); name the actual relationship.\n"
    "- No shallow -ing phrases ('symbolizing', 'reflecting', 'showcasing').\n"
    "- No sales language ('breathtaking', 'must-visit', 'stunning', 'nestled', 'renowned').\n"
    "- No borrowed authority ('experts believe', 'cited in NYT/BBC/FT'); name a real source and what it said, or cut it.\n"
    "- Prefer 'is / are / has' over 'serves as', 'features', 'boasts'.\n"
    "- No chatbot residue ('I hope this helps', 'Great question!').\n"
    "- No knowledge-limit disclaimers ('based on available information', 'details are limited').\n"
    "- Use plain, specific, concrete words. Vary sentence length. Write for one reader.\n\n"
    "LENGTH: keep full detail — preserve key figures, context, and specifics. Do not over-truncate.\n"
    "FORMAT: use short paragraphs separated by one blank line. Do not cram everything into one dense block.\n\n"
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
