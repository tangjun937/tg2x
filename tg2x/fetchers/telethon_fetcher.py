"""telethon 后端：MTProto 用户会话。

适用：① 频道是私有的 / 关闭了网页预览；② 要求秒级实时。
准备：
  1) 到 https://my.telegram.org/apps 申请 api_id / api_hash
  2) 填到 .env：TG_API_ID / TG_API_HASH / TG_PHONE
  3) pip install telethon
  4) 首次运行 `python run.py login`，输入手机验证码（有二级密码再输一次），
     之后会话存到 data/tg2x.session，不再需要验证码。
"""
from __future__ import annotations

from ..models import TgPost, parse_tg_time
from .base import Fetcher


class TelethonFetcher(Fetcher):
    name = "telethon"

    def __init__(self, channel: str, api_id: int, api_hash: str,
                 session_path: str, phone: str = ""):
        try:
            from telethon.sync import TelegramClient  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "未安装 telethon，请先执行：pip install telethon"
            ) from e
        from telethon.sync import TelegramClient

        self.channel = channel.lstrip("@")
        self.phone = phone
        self.client = TelegramClient(session_path, api_id, api_hash)

    def connect(self) -> "TelethonFetcher":
        self.client.connect()
        if not self.client.is_user_authorized():
            if not self.phone:
                raise RuntimeError("会话未登录，且未提供 TG_PHONE，无法完成登录")
            self.client.start(phone=self.phone)
        return self

    def fetch(self, limit: int = 20) -> list[TgPost]:
        if not self.client.is_connected():
            self.connect()
        entity = self.client.get_entity(self.channel)
        out: list[TgPost] = []
        for msg in self.client.get_messages(entity, limit=limit):
            if msg is None:
                continue
            text = msg.message or ""
            media_urls: list[str] = []
            media_files: list[str] = []
            if getattr(msg, "photo", None) or getattr(msg, "document", None):
                # 媒体需要下载才有文件；这里先标记，由 pipeline 决定是否下载
                media_urls.append(f"tg-media://{self.channel}/{msg.id}")
            out.append(
                TgPost(
                    channel=self.channel,
                    message_id=msg.id,
                    text=text,
                    posted_at=parse_tg_time(msg.date.isoformat() if msg.date else ""),
                    url=f"https://t.me/{self.channel}/{msg.id}",
                    media_urls=media_urls,
                    media_files=media_files,
                    source=self.name,
                )
            )
        out.sort(key=lambda p: p.message_id)
        return out[-limit:]

    def download_media(self, message_id: int, dest: str) -> str | None:
        """按需下载单条消息的媒体到本地，返回文件路径。"""
        if not self.client.is_connected():
            self.connect()
        entity = self.client.get_entity(self.channel)
        msg = self.client.get_messages(entity, ids=message_id)
        if not msg:
            return None
        return self.client.download_media(msg, file=dest)

    def close(self) -> None:
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
