"""统一的消息数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class TgPost:
    """从 Telegram 抓到的一条原始帖子（未翻译、未发布）。"""

    channel: str
    message_id: int
    text: str = ""
    posted_at: str = ""
    url: str = ""
    media_urls: list[str] = field(default_factory=list)
    media_files: list[str] = field(default_factory=list)
    has_video: bool = False
    has_photo: bool = False
    raw_html: str = ""
    source: str = ""  # web | telethon | bot

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.message_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw_html", None)  # 归档时单独存，不重复塞进 JSON
        return d


@dataclass
class TransPost:
    """翻译后、准备发到 X 的成品。"""

    post: TgPost
    text_en: str
    final_text: str = ""

    @property
    def key(self) -> str:
        return self.post.key


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_tg_time(value: str) -> str:
    """尽量把各种时间格式统一成 ISO8601 本地时间；失败则原样返回。"""
    if not value:
        return now_iso()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="seconds")
    except Exception:
        return value
