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
    "You are a news writer posting to X (Twitter). Turn the news into ONE short, punchy "
    "ENGLISH opinion post that reads like a real person talking, not an AI. Always write "
    "in English, even if the source text is Chinese or Russian.\n\n"
    "ONLY OPINION: Give just your take — no news recap, no 'the report found...', no "
    "restating the facts. Pick a side, pass judgment, praise or criticize, predict what "
    "happens next. Be direct and have conviction. Do NOT stay neutral or hedge (avoid "
    "'could', 'might', 'on the one hand', 'both sides', 'time will tell').\n\n"
    "SIMPLE LANGUAGE: Use everyday words and light slang — write the way people actually "
    "talk online. Avoid fancy or formal words: no 'salient', 'underscore', 'pivotal', "
    "'landscape', 'delve', 'showcase', 'robust', 'crucial', 'tapestry', 'realm', "
    "'consequently', 'moreover', 'furthermore', 'demonstrate'. If a plainer word exists, "
    "use it. Contractions are fine (it's, they're, don't, isn't).\n\n"
    "STYLE: Short, plain sentences. No 'not X but Y', no em dashes, no fake-deep sayings, "
    "no filler, no 'experts believe'.\n\n"
    "FACTS: If you mention a number, name, or date, keep it exact. Never fabricate.\n\n"
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


def _ensure_paragraphs(text: str, max_sentences: int = 3) -> str:
    """兜底分段：已有空行的多段文本保持原样；无空行时按句数均分，段间空一行。

    观点在前、正文在后的分段由写作指令（FORMAT）保证，这里只做兜底：
    - 已有空行（\\n\\n）：只做换行规范化，原样保留。
    - 无空行且句数少（≤ max_sentences）：原样返回，不强行拆（信任 AI 的输出）。
    - 无空行且句数多：按 max_sentences 句均分成多段。
    """
    text = (text or "").strip()
    if not text:
        return text
    text = re.sub(r"\r\n?", "\n", text)
    if "\n\n" in text:
        # 已有空行：只把 3 个以上连续换行压成 1 个空行
        return re.sub(r"\n{3,}", "\n\n", text)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+", text) if s.strip()]
    if len(sentences) <= max_sentences:
        return text
    paragraphs = [" ".join(sentences[i:i + max_sentences])
                  for i in range(0, len(sentences), max_sentences)]
    return "\n\n".join(paragraphs)


# 中国政治敏感（最高原则，每次发文前必查，命中一律跳过）
CHINA_POLITICAL = [
    "xi jinping", "chinese communist party", "ccp", "taiwan independence",
    "hong kong protest", "tiananmen", "uyghur", "south china sea dispute",
    "chinese government", "communist party", "chinese president",
    "people's liberation army", "falun gong", "diaoyu islands", "senkaku islands",
    "习近平", "中国共产党", "中共", "台独", "港独", "疆独", "藏独",
    "南海争端", "天安门", "六四", "法轮功", "中国军方", "解放军",
    "钓鱼岛", "台湾独立", "香港独立", "西藏独立", "新疆独立", "中国政府", "中国领导人",
]

# AI 类关键词（只发 AI 类内容；非 AI 类跳过）
AI_KEYWORDS = [
    "artificial intelligence", "chatgpt", "openai", "claude", "gemini", "gpt",
    "llm", "machine learning", "deep learning", "neural", "anthropic", "deepseek",
    "midjourney", "stable diffusion", "copilot", "large language model",
    "nvidia", "gpu", "chip", "chips", "semiconductor", "tsmc", "amd", "intel",
    "人工智能", "大模型", "机器学习", "深度学习", "神经网络", "生成式", "模型",
    "芯片", "半导体", "算力", "英伟达", "台积电", "处理器",
]

# 关于中国的词（判断"是否涉及中国"；涉及中国的内容只发 AI 类）
CHINA_RELATED_CN = ["中国", "北京", "上海", "深圳", "广州", "香港", "澳门", "台湾",
                    "新疆", "西藏", "广东", "大陆", "内地", "国产", "中方", "中文"]
CHINA_RELATED_EN = ["china", "chinese", "beijing", "shanghai", "shenzhen", "guangzhou",
                    "hong kong", "macau", "taiwan", "xinjiang", "tibet", "guangdong"]

_AI_WORD_RE = re.compile(r"\bai\b", re.IGNORECASE)
_AGI_WORD_RE = re.compile(r"\bagi\b", re.IGNORECASE)


def _is_china_related(text: str) -> bool:
    """判断内容是否涉及中国（中文子串 + 英文词边界）。"""
    low = (text or "").lower()
    if any(w in low for w in CHINA_RELATED_CN):
        return True
    for w in CHINA_RELATED_EN:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def _is_ai(text: str) -> bool:
    """判断内容是否 AI 类。"""
    low = (text or "").lower()
    return (_AI_WORD_RE.search(text or "") or _AGI_WORD_RE.search(text or "")
            or any(w in low for w in AI_KEYWORDS))


def content_filter_reason(text: str) -> str:
    """内容合规过滤，返回原因（空串=通过）。

    最高原则：中国政治/主权敏感内容，一律不发。
    关于中国的内容（非政治）：只发 AI 类，非 AI 类不发。
    非中国的内容：正常发（不限 AI 类）。
    """
    low = (text or "").lower()
    if any(w in low for w in CHINA_POLITICAL):
        return "china-political"
    if _is_china_related(text) and not _is_ai(text):
        return "china-non-ai"
    return ""


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
            out = self._chat(WRITE_SYSTEM, (text or "").strip()).strip()
            return _ensure_paragraphs(out)
        except Exception as e:  # noqa: BLE001
            log.warning("DeepSeek 写作失败：%s", e)
            return ""

    def rank(self, posts, topic_first: bool = False) -> list:
        """先按优先级排序（媒体/主题），同优先级内由大模型精排影响大小。

        默认（topic_first=False）：媒体优先 视频 > 图文 > 无图冲突事件 > 普通事件。
        topic_first=True：主题优先 github > 传统金融 > 虚拟货币，同主题内再按媒体。
        """
        if len(posts) <= 1:
            return posts
        if topic_first:
            key = lambda p: (_topic_level(p.text), _media_level(p))
        else:
            key = lambda p: (_media_level(p),)
        posts = sorted(posts, key=key)
        if not self.available():
            return posts
        # 同优先级分组，组内用大模型按影响精排
        groups: list[list] = []
        current = [posts[0]]
        for p in posts[1:]:
            if key(p) == key(current[0]):
                current.append(p)
            else:
                groups.append(current)
                current = [p]
        groups.append(current)
        result: list = []
        for g in groups:
            result.extend(self._rank_by_impact(g))
        return result

    def _rank_by_impact(self, posts) -> list:
        """组内用 DeepSeek 按影响大小排序。失败返回原顺序。"""
        if len(posts) <= 1:
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
            log.warning("DeepSeek 组内影响排序失败：%s", e)
            return posts
