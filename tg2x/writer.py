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

# 冲突事件关键词（中英）：无图时优先于普通事件
CONFLICT_WORDS = [
    "war", "conflict", "attack", "strike", "protest", "riot", "clash", "bombing",
    "invasion", "killed", "shooting", "coup", "battle", "missile", "airstrike",
    "ceasefire", "casualt", "explosion",
    "战争", "冲突", "袭击", "罢工", "抗议", "暴乱", "爆炸", "入侵", "枪击", "政变",
    "导弹", "空袭", "伤亡", "交火", "停火",
]

# 主题分类关键词（需求：github > 传统金融 > 虚拟货币）
TOPIC_GITHUB = ["github", "repository", "open source", "open-source", "repo",
                "pull request", "gitlab", "开源", "代码仓库", "代码"]
TOPIC_FINANCE = ["stock", "stocks", "equity", "bond", "bonds", "bank", "federal reserve",
                 "interest rate", "ipo", "earnings", "wall street", "nasdaq", "s&p",
                 "股市", "股票", "利率", "财报", "央行", "联储", "债券", "银行", "交易所", "基金"]
TOPIC_CRYPTO = ["crypto", "bitcoin", "btc", "ethereum", "eth", "token", "blockchain",
                "defi", "stablecoin", "币", "比特币", "加密货币", "以太坊", "稳定币", "区块链",
                "数字货币", "钱包"]


def _media_level(post) -> int:
    """媒体优先级：视频0 > 图文1 > 无图冲突事件2 > 普通事件3。"""
    if getattr(post, "has_video", False):
        return 0
    if getattr(post, "has_photo", False):
        return 1
    low = (post.text or "").lower()
    if any(w in low for w in CONFLICT_WORDS):
        return 2
    return 3


def _topic_level(text) -> int:
    """主题优先级：github0 > 传统金融1 > 虚拟货币2 > 其他3。"""
    low = (text or "").lower()
    if any(w in low for w in TOPIC_GITHUB):
        return 0
    if any(w in low for w in TOPIC_FINANCE):
        return 1
    if any(w in low for w in TOPIC_CRYPTO):
        return 2
    return 3


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

    def rank(self, posts, topic_first: bool = False) -> list:
        """按优先级排序。

        默认（topic_first=False）：媒体优先 视频 > 图文 > 无图冲突事件 > 普通事件。
        topic_first=True：主题优先 github > 传统金融 > 虚拟货币，同主题内再按媒体。
        同优先级按 message_id 从新到旧。
        """
        if len(posts) <= 1:
            return posts
        if topic_first:
            key = lambda p: (_topic_level(p.text), _media_level(p), -p.message_id)
        else:
            key = lambda p: (_media_level(p), -p.message_id)
        return sorted(posts, key=key)
