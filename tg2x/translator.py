"""中 -> 英 翻译层。

两种 provider：
  google_free —— 免费接口，无需 key，默认；
  llm         —— OpenAI 兼容接口，新闻口吻更自然，需要 API key。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time

import requests

from .config import Config

log = logging.getLogger("tg2x")

CJK = re.compile(r"[\u4e00-\u9fff]")
BLANK = re.compile(r"\n{3,}")
URL_RE = re.compile(r"https?://\S+")
# 只认「独立成词」的话题标签：#沙巴 / #Thailand，不吞 #网友投稿：xxx 这种中文标注
TAG_RE = re.compile(r"(?<!\S)#([^\s#：:，,。.！!？?、；;（）()\[\]【】\"'…~]{1,20})(?!\S)")

# 频道自带的推广尾巴，默认不进译文
DEFAULT_REMOVE = [
    r"关注[^\n]*?频道[^\n]*",
    r"订阅[^\n]*(?:频道|曝光)[^\n]*",
    r"投稿[^\n]{0,6}[:：]\s*@\w+",
    r"商务合作\s*[:：][^\n]*",
    r"^\s*[➡️👉]\s*[^\n]*$",
    r"^\s*@\w+\s*$",
]

# 频道每条快讯第一行是【标题/抬头】，按需整行去掉、只留正文（见 cleanup.remove_heading）
HEADING_RE = re.compile(r"^\s*【[^】]*】\s*$", flags=re.MULTILINE)

# 用于「从末尾剥推广行」的判断
PROMO_LEAD = "📢📣🔗😘➡️👉🔔💬✈️🚀📌📍👇🎯🆔"
PROMO_HEADS = ("订阅", "投稿", "爆料", "商务", "合作", "加群", "广告", "推广", "关注",
               "点击", "加入", "进群", "分享给")


def _is_promo_line(line: str) -> bool:
    """只有明确的推广特征才算，避免把正文短句误删。"""
    s = line.strip()
    if not s:
        return True
    if s[0] in PROMO_LEAD:                     # emoji 开头的推广行
        return True
    if re.search(r"t\.me/|telegram\.me/", s, re.IGNORECASE):  # 带频道链接
        return True
    if re.fullmatch(r"@\w{3,}", s):            # 单独一行的 @账号
        return True
    if len(s) <= 40 and s.startswith(PROMO_HEADS):  # 「关注…/订阅…/投稿…」开头
        return True
    return False


SYSTEM_PROMPT = (
    "You are a professional news translator. Translate the Chinese social media post "
    "into concise, natural English suitable for X (Twitter). Rules: keep the meaning "
    "exact, do not add facts, do not editorialize, keep names of people/places in "
    "pinyin or their common English form, keep emoji, keep line breaks, no preamble, "
    "output only the translation."
)

REWRITE_SYSTEM = (
    "You are a news editor. Rewrite the given English news snippet so it expresses "
    "the same facts in a different, natural way (paraphrase). Rules: keep every fact, "
    "number, and name exactly; do not add or remove information; do not editorialize; "
    "keep names of people/places/organizations unchanged; keep emoji and line breaks; "
    "no preamble; output only the rewritten text."
)


class Translator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.provider = (cfg.get("translate.provider") or "google_free").lower()
        self.timeout = int(cfg.get("translate.timeout", 30) or 30)
        # 两次翻译请求之间的最小间隔（秒），用于降低免费接口 429 概率
        self.min_interval = float(cfg.get("translate.min_interval", 1.0) or 0)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        })

    # ---------- 对外 ----------
    def translate(self, text: str) -> str:
        text = self._clean(text or "")
        if not text:
            return ""
        tags: list[str] = []
        if self.cfg.get("translate.keep_hashtags", False):
            tags = [m.group(0) for m in TAG_RE.finditer(text)]
            text = TAG_RE.sub("", text).strip()
        if self.cfg.get("translate.strip_urls_from_translate", True):
            text = URL_RE.sub("", text).strip()

        if not CJK.search(text):
            out = text  # 本来就是英文
        elif self.provider == "llm":
            out = self._llm(text)
        else:
            out = self._google(text)

        out = BLANK.sub("\n\n", out).strip()
        if tags:
            out = f"{out}\n\n{' '.join(dict.fromkeys(tags))}".strip()
        prefix = self.cfg.get("translate.prefix", "") or ""
        return f"{prefix}{out}".strip()

    # ---------- 洗稿（LLM paraphrase） ----------
    def rewrite(self, text: str) -> str:
        """英文洗稿：把原文换一种表达。失败/未配置 key 时退回清理后的原文。"""
        text = self._clean_en(text or "")
        if not text or not self.cfg.get("rewrite.enabled", False):
            return text
        base = (self.cfg.get("rewrite.base_url") or "").rstrip("/")
        key = self.cfg.env(self.cfg.get("rewrite.api_key_env", "DEEPSEEK_API_KEY"))
        if not key:
            log.warning("rewrite 已启用但未配置 %s，退回原文",
                       self.cfg.get("rewrite.api_key_env", "DEEPSEEK_API_KEY"))
            return text
        model = self.cfg.get("rewrite.model", "deepseek-chat")
        temperature = float(self.cfg.get("rewrite.temperature", 0.7) or 0.7)
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": text},
            ],
        }
        try:
            r = self.session.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=int(self.cfg.get("rewrite.timeout", 60) or 60),
            )
            if r.status_code >= 400:
                log.warning("LLM 洗稿失败 HTTP %s: %s，退回原文", r.status_code, r.text[:200])
                return text
            out = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
            out = (out or "").strip()
            return out or text
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 洗稿异常 %s，退回原文", e)
            return text

    def _clean_en(self, text: str) -> str:
        """英文频道尾巴清理：去 Source/来源 行、去 URL、去 Binance 等推广行。"""
        lines = [ln.strip() for ln in text.split("\n")]
        out: list[str] = []
        for ln in lines:
            if not ln:
                continue
            if re.match(r"^(Source|Via|来源)\s*[:：]", ln, re.IGNORECASE):
                continue
            if re.search(r"binance", ln, re.IGNORECASE):
                continue
            if URL_RE.fullmatch(ln):
                continue  # 纯链接行
            out.append(ln)
        text = "\n".join(out).strip()
        text = URL_RE.sub("", text).strip()  # 去掉行内残留 URL
        return BLANK.sub("\n\n", text).strip()

    def _clean(self, text: str) -> str:
        """去掉频道推广尾巴 / 投稿联系方式等噪音。"""
        if self.cfg.get("cleanup.remove_heading", False):
            text = HEADING_RE.sub("", text, count=1).strip()

        patterns = self.cfg.get("cleanup.remove_patterns", None)
        if patterns is None:
            patterns = DEFAULT_REMOVE
        for pat in patterns:
            try:
                text = re.sub(pat, "", text, flags=re.MULTILINE)
            except re.error:
                continue

        lines = [ln.strip() for ln in text.split("\n")]
        lines = [ln for ln in lines if ln]

        # 从末尾逐行剥掉推广块（最多 6 行），正文里的关键词不受影响
        if self.cfg.get("cleanup.tail_trim", True):
            removed = 0
            while (len(lines) > 1 and removed < 6
                   and _is_promo_line(lines[-1])):
                lines.pop()
                removed += 1

        return BLANK.sub("\n\n", "\n".join(lines)).strip()


    # ---------- google_free ----------
    def _google(self, text: str) -> str:
        chunks = _chunk(text, 1500)
        parts = []
        try:
            for i, c in enumerate(chunks):
                if i and self.min_interval > 0:
                    time.sleep(self.min_interval)  # 节流，降低 429 概率
                parts.append(self._google_one(c))
        except Exception:
            fb = (self.cfg.get("translate.fallback") or "").strip()
            if fb == "mymemory":
                log.warning("Google 翻译不可用，切换到 MyMemory 备用引擎")
                return self._mymemory(text)
            raise
        return "\n".join(parts)

    # 两个免费端点：clients5 现在对脚本更友好（gtx 常被 429）
    ENDPOINTS = (
        ("https://clients5.google.com/translate_a/t",
         {"client": "dict-chrome-ex", "sl": "auto", "tl": "en"}),
        ("https://translate.googleapis.com/translate_a/single",
         {"client": "gtx", "sl": "auto", "tl": "en", "dt": "t"}),
    )

    def _google_one(self, text: str) -> str:
        last_err: object = "unknown"
        for url, base_params in self.ENDPOINTS:
            for attempt in range(2):
                try:
                    params = dict(base_params, q=text)
                    r = self.session.get(url, params=params, timeout=self.timeout)
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {r.status_code}"
                    else:
                        r.raise_for_status()
                        return _parse_translate(r.json())
                except Exception as e:  # noqa: BLE001
                    last_err = e
                time.sleep(min(1.5 * (2 ** attempt), 6) + random.random())
        raise RuntimeError(f"Google 翻译失败（两个端点均已重试）：{last_err}")

    # ---------- 备用引擎 ----------
    def _mymemory(self, text: str) -> str:
        """MyMemory 免费接口，匿名有每日额度，仅作应急。"""
        out = []
        for piece in _chunk(text, 400):
            if not CJK.search(piece):
                out.append(piece)
                continue
            r = self.session.get(
                "https://api.mymemory.translated.net/get",
                params={"q": piece, "langpair": "zh-CN|en"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            seg = (data.get("responseData") or {}).get("translatedText")
            if not seg:
                raise RuntimeError(f"MyMemory 返回异常：{str(data)[:200]}")
            out.append(seg)
        return "\n".join(out)


    # ---------- llm ----------
    def _llm(self, text: str) -> str:
        base = (self.cfg.get("translate.base_url") or "").rstrip("/")
        key = self.cfg.env(self.cfg.get("translate.api_key_env", "OPENAI_API_KEY"))
        if not key:
            raise RuntimeError("translate.provider=llm 但未配置 API key")
        model = self.cfg.get("translate.model", "gpt-4o-mini")
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        r = self.session.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=max(self.timeout, 60),
        )
        if r.status_code >= 400:
            raise RuntimeError(f"LLM 翻译失败 HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"].strip()


def _parse_translate(data) -> str:
    """兼容两个端点的返回格式。

    clients5 : [["译文", "zh-CN"]]
    gtx      : [[["译文", "原文", ...], ...], ...]
    """
    if not isinstance(data, list) or not data:
        raise ValueError(f"翻译返回格式异常：{str(data)[:120]}")

    first = data[0]
    # clients5：data[0] = [译文, 语言]
    if isinstance(first, list) and first and isinstance(first[0], str):
        return "".join(x[0] for x in data if isinstance(x, list) and x and isinstance(x[0], str))
    # gtx：data[0] = [[译文, 原文, ...], ...]
    if isinstance(first, list) and first and isinstance(first[0], list):
        return "".join(s[0] for s in first if isinstance(s, list) and s and s[0])
    # 纯字符串数组
    if isinstance(first, str):
        return "".join(x for x in data if isinstance(x, str))
    raise ValueError(f"翻译返回格式异常：{str(data)[:120]}")


def _chunk(text: str, size: int) -> list[str]:
    """按行切块，避免把一句话切断。"""
    if len(text) <= size:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        while len(line) > size:  # 超长单行硬切
            chunks.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) + 1 > size:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]
