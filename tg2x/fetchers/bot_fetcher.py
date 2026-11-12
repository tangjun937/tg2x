"""bot 后端：Telegram Bot API。

适用：你是频道主 / 管理员，追求实时性和长期稳定。
准备：
  1) 找 @BotFather 建 bot，拿 token
  2) 把 bot 拉进频道并设为管理员（只需「发布消息」权限即可收到 channel_post）
  3) .env 里填 TG_BOT_TOKEN，config.yaml 里 fetch.backend 改成 bot
特点：Bot API 只能收到「成为管理员之后的」新帖，拿不到历史；offset 会存进 sqlite 断点续传。
"""
from __future__ import annotations

import requests

from ..models import TgPost, parse_tg_time
from .base import Fetcher


class BotFetcher(Fetcher):
    name = "bot"

    def __init__(self, channel: str, token: str, offset: int = 0, timeout: int = 20):
        self.channel = channel.lstrip("@").lower()
        self.token = token
        self.timeout = timeout
        self.offset = offset
        self.api = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def health(self) -> tuple[bool, str]:
        try:
            r = self.session.get(f"{self.api}/getMe", timeout=self.timeout).json()
            if not r.get("ok"):
                return False, f"getMe 失败：{r.get('description')}"
            me = r["result"]
            return True, f"bot @{me.get('username')} 可用"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def fetch(self, limit: int = 20) -> list[TgPost]:
        params = {"timeout": 0, "allowed_updates": '["channel_post"]'}
        if self.offset:
            params["offset"] = self.offset
        r = self.session.get(f"{self.api}/getUpdates", params=params,
                             timeout=self.timeout).json()
        if not r.get("ok"):
            raise RuntimeError(f"getUpdates 失败：{r.get('description')}")

        out: list[TgPost] = []
        for upd in r["result"]:
            self.offset = max(self.offset, upd["update_id"] + 1)
            post = upd.get("channel_post")
            if not post:
                continue
            chat = post.get("chat", {})
            username = (chat.get("username") or "").lower()
            # 只处理目标频道（用 username 或 chat_id 都能对上）
            if username and username != self.channel:
                continue
            text = post.get("text") or post.get("caption") or ""
            media: list[str] = []
            if post.get("photo"):
                media.append(f"tg-file://{post['photo'][-1]['file_id']}")
            if post.get("video"):
                media.append(f"tg-file://{post['video']['file_id']}")
            out.append(
                TgPost(
                    channel=chat.get("username") or self.channel,
                    message_id=post["message_id"],
                    text=text,
                    posted_at=parse_tg_time(str(post.get("date", ""))),
                    url=f"https://t.me/{chat.get('username') or self.channel}/{post['message_id']}",
                    media_urls=media,
                    source=self.name,
                )
            )
        out.sort(key=lambda p: p.message_id)
        return out[-limit:]
