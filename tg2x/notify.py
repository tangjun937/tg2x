"""Telegram Bot 通知层。

说明：你不是 @YMISH3333 的管理员，bot 进不了那个频道，所以 Bot API 读不到源频道内容。
bot 在这里的角色是「通知/回执通道」：
  - 每次跑完告诉你发了几条、失败了没有
  - 失败时把错误原因直接推给你，不用去翻 Actions 日志
另外可以顺手把发出去的英文正文推给你，当成一个预览。

凭据：TG_BOT_TOKEN（@BotFather 给的）、TG_CHAT_ID（你自己的私聊 id，用 `python run.py bot-id` 查）
"""
from __future__ import annotations

import logging

import requests

from .config import Config

log = logging.getLogger("tg2x")
API = "https://api.telegram.org/bot{token}/{method}"


class TgNotifier:
    def __init__(self, cfg: Config):
        self.enabled_cfg = bool(cfg.get("notify.enabled", True))
        self.token = cfg.env(cfg.get("notify.bot_token_env", "TG_BOT_TOKEN"))
        self.chat_id = cfg.env(cfg.get("notify.chat_id_env", "TG_CHAT_ID"))
        self.on_success = bool(cfg.get("notify.on_success", True))
        self.on_failure = bool(cfg.get("notify.on_failure", True))
        self.include_text = bool(cfg.get("notify.include_text", True))
        self.quiet_when_noop = bool(cfg.get("notify.quiet_when_noop", True))
        self.timeout = int(cfg.get("notify.timeout", 20) or 20)
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.enabled_cfg and self.token and self.chat_id)

    # ---------- 底层 ----------
    def send(self, text: str) -> bool:
        if not self.enabled:
            log.debug("通知未配置，跳过")
            return False
        for chunk in _split(text, 3800):
            try:
                r = self.session.post(
                    API.format(token=self.token, method="sendMessage"),
                    data={"chat_id": self.chat_id, "text": chunk,
                          "disable_web_page_preview": "true"},
                    timeout=self.timeout,
                )
                if r.status_code >= 400:
                    log.warning("Telegram 通知失败 HTTP %s: %s", r.status_code, r.text[:200])
                    return False
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram 通知异常: %s", e)
                return False
        return True

    # ---------- 业务回执 ----------
    def report(self, result: dict, items: list[dict] | None = None) -> None:
        """result 来自 Pipeline.cycle()；items 是每条处理的摘要（含失败原因）。"""
        if not self.enabled:
            return
        new = result.get("new", 0)
        failed = result.get("failed", 0)
        published = result.get("published", 0)
        items = items or []
        ok_items = [i for i in items if not i.get("error")]
        bad_items = [i for i in items if i.get("error")]

        if new == 0 and self.quiet_when_noop:
            return  # 没新帖就闭嘴，别每次定时任务都来一条

        lines: list[str] = []
        if published:
            lines.append(f"✅ 已发布 {published} 条到 X（经 Buffer）")
        if failed and self.on_failure:
            lines.append(f"⚠️ 失败 {failed} 条，以下内容没有发出去：")
            for it in bad_items:
                lines.append(f"· {it.get('key')}: {it['error'][:180]}")
        if not published and not failed:
            lines.append("本轮只有广告/重复内容，已全部跳过，没有发布。")

        if published and self.include_text and self.on_success:
            for it in ok_items[:3]:
                lines.append("")
                lines.append(f"— {it.get('key')} —")
                lines.append((it.get("text") or "")[:600])

        if lines:
            self.send("\n".join(lines))


def _split(text: str, n: int) -> list[str]:
    if len(text) <= n:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > n:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def discover_chats(token: str, timeout: int = 20) -> list[dict]:
    """读取 bot 最近收到的消息，把 chat_id 找出来（需要你先给 bot 发一句 /start）。"""
    r = requests.get(API.format(token=token, method="getUpdates"),
                     params={"timeout": 0}, timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates 失败：{data.get('description')}")
    seen: dict[int, dict] = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if not chat.get("id"):
            continue
        seen[chat["id"]] = {
            "id": chat["id"],
            "type": chat.get("type"),
            "name": chat.get("username") or chat.get("title") or chat.get("first_name") or "",
        }
    return list(seen.values())
