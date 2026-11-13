"""发布层：把成品交给下游。

四个 target：
  buffer  —— 走 Buffer GraphQL API（你现在的路子：Buffer 再发到 X）【默认】
  command —— 调用任意脚本，JSON 从 stdin 喂进去
  http    —— POST JSON 到你自己的发文服务
  dry_run —— 只打印，不发

Buffer 的限制：不支持文件上传，图片必须是公网 URL。
所以这里默认把 Telegram CDN 的原图链接直接传过去（`publish.buffer.media: telegram_url`），
不需要自建图床。
"""
from __future__ import annotations

import json
import logging
import random
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("tg2x")

from .buffer_client import (
    BufferClient,
    BufferError,
    image_assets,
    load_channel_cache,
    save_channel_cache,
)
from .config import Config
from .models import TransPost


@dataclass
class PublishResult:
    ok: bool
    tweet_id: str = ""
    detail: str = ""


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.target = (cfg.get("publish.target") or "dry_run").lower()
        self.max_len = int(cfg.get("publish.max_len", 280) or 280)
        self.add_link = bool(cfg.get("publish.include_source_link", True))
        self.dry_run = bool(cfg.get("publish.dry_run", False)) or self.target == "dry_run"
        self._buf: BufferClient | None = None

    # ---------- 正文拼装 ----------
    def build_text(self, item: TransPost) -> str:
        body = (item.text_en or "").strip()
        link = item.post.url if self.add_link and item.post.url else ""
        if not link:
            return _clip_weighted(body, self.max_len)
        # X 的 t.co 短链固定按 23 字计权，这里留够并多留 3 字余量
        room = self.max_len - 23 - 3
        return f"{_clip_weighted(body, max(room, 40))}\n\n{link}"

    def payload(self, item: TransPost) -> dict:
        p = item.post
        return {
            "text": item.final_text or self.build_text(item),
            "text_en": item.text_en,
            "text_original": p.text,
            "source": "telegram",
            "source_channel": p.channel,
            "source_message_id": p.message_id,
            "source_url": p.url,
            "source_posted_at": p.posted_at,
            "media_files": p.media_files,
            "media_urls": p.media_urls,
            "dedupe_key": p.key,
        }

    # ---------- 发送 ----------
    def publish(self, item: TransPost, position: int = 0) -> PublishResult:
        if not item.final_text:
            item.final_text = self.build_text(item)

        if self.dry_run:
            return PublishResult(True, "", "DRY-RUN 未真实发送")

        if self.target == "buffer":
            return self._buffer(item, position=position)
        if self.target == "command":
            return self._command(self.payload(item))
        if self.target == "http":
            return self._http(self.payload(item))
        raise RuntimeError(f"未知 publish.target: {self.target}")

    # ---------- Buffer ----------
    def _buffer_client(self) -> BufferClient:
        if self._buf is None:
            token = self.cfg.env(self.cfg.get("publish.buffer.token_env", "BUFFER_TOKEN"))
            self._buf = BufferClient(
                token,
                timeout=int(self.cfg.get("publish.buffer.timeout", 30) or 30),
            )
        return self._buf

    def resolve_buffer_channel(self) -> str:
        """依次尝试：配置指定 -> 本地缓存 -> 自动查询。"""
        explicit = (self.cfg.get("publish.buffer.channel_id") or "").strip()
        if explicit:
            return explicit

        cache_path = self.cfg.path_of("storage.data_dir", "data") / "buffer_channel.json"
        cached = load_channel_cache(cache_path)
        if cached and cached.get("channelId"):
            return cached["channelId"]

        org_id, channel = self._buffer_client().find_x_channel()
        save_channel_cache(cache_path, org_id, channel)
        return channel["id"]

    def _buffer(self, item: TransPost, position: int = 0) -> PublishResult:
        try:
            client = self._buffer_client()
            channel_id = self.resolve_buffer_channel()
            media_mode = (self.cfg.get("publish.buffer.media") or "telegram_url").lower()
            assets = []
            if media_mode == "telegram_url":
                assets = image_assets(
                    item.post.media_urls,
                    limit=int(self.cfg.get("publish.buffer.max_assets", 4) or 4),
                )
            mode = self.cfg.get("publish.buffer.mode", "shareNow")
            due_at = None
            # 同批次的第 2 条往后，排到 10-15 分钟后的随机时刻，避免瞬间连发被 X 判刷屏
            if position > 0:
                lo = int(self.cfg.get("publish.buffer.spacing_min_minutes", 10) or 10)
                hi = int(self.cfg.get("publish.buffer.spacing_max_minutes", 15) or 15)
                if hi < lo:
                    hi = lo
                delay = random.randint(lo, hi)
                due = datetime.now(timezone.utc) + timedelta(minutes=delay)
                due_at = due.strftime("%Y-%m-%dT%H:%M:%SZ")
                mode = "customScheduled"
                log.info("同批第 %d 条排到 %d 分钟后发布（%s）", position + 1, delay, due_at)
            post = client.create_post(
                channel_id=channel_id,
                text=item.final_text,
                assets=assets,
                mode=mode,
                due_at=due_at,
            )
            return PublishResult(True, str(post.get("id", "")),
                                 f"buffer post {post.get('id')} dueAt={post.get('dueAt')}")
        except BufferError as e:
            return PublishResult(False, "", str(e))
        except Exception as e:  # noqa: BLE001
            return PublishResult(False, "", f"{type(e).__name__}: {e}")

    # ---------- 通用适配器 ----------
    def _command(self, payload: dict) -> PublishResult:
        cmd = self.cfg.get("publish.command.command") or ""
        if not cmd:
            raise RuntimeError("publish.target=command 但未配置 publish.command.command")
        proc = subprocess.run(
            cmd, shell=True,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180,
        )
        if proc.returncode != 0:
            return PublishResult(False, "", f"exit={proc.returncode} stderr={proc.stderr[-400:]}")
        tweet_id = ""
        out = (proc.stdout or "").strip()
        if out:
            try:
                tweet_id = str(json.loads(out).get("tweet_id", ""))
            except Exception:  # noqa: BLE001
                tweet_id = ""
        return PublishResult(True, tweet_id, out[-200:])

    def _http(self, payload: dict) -> PublishResult:
        url = self.cfg.get("publish.http.url") or ""
        if not url:
            raise RuntimeError("publish.target=http 但未配置 publish.http.url")
        token = self.cfg.env(self.cfg.get("publish.http.token_env", "X_POSTER_TOKEN"))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = requests.post(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, timeout=120)
        if r.status_code >= 400:
            return PublishResult(False, "", f"HTTP {r.status_code}: {r.text[:300]}")
        tweet_id = ""
        try:
            tweet_id = str(r.json().get("tweet_id", ""))
        except Exception:  # noqa: BLE001
            tweet_id = ""
        return PublishResult(True, tweet_id, r.text[:200])


def _clip(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    cut = text[: max(n - 1, 0)]
    for sep in ("\n", " ", "，", ","):
        idx = cut.rfind(sep)
        if idx > len(cut) * 0.6:
            cut = cut[:idx]
            break
    return cut.rstrip(" ,，、") + "…"


# X（twitter-text）的字符计权：这几段区间按 1 计，其余（中日韩文字、emoji）按 2 计。
# 之前只按 len() 截断，残留的中文/表情会让实际计权超过 280，Buffer 直接拒收。
_ONE_WEIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
_ELLIPSIS = "…"


def x_weight(text: str) -> int:
    """按 X 的规则算一条推文的"字数"。"""
    n = 0
    for ch in text:
        o = ord(ch)
        for lo, hi in _ONE_WEIGHT_RANGES:
            if lo <= o <= hi:
                n += 1
                break
        else:
            n += 2
    return n


def _clip_weighted(text: str, limit: int) -> str:
    """按 X 计权截断，保证 Buffer 侧不会因超字数被拒。"""
    text = (text or "").strip()
    if limit <= 0 or x_weight(text) <= limit:
        return text
    budget = limit - x_weight(_ELLIPSIS)
    out: list[str] = []
    used = 0
    for ch in text:
        w = x_weight(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    cut = "".join(out)
    # 尽量在换行/句子/单词边界收尾，避免半个单词
    for sep in ("\n", ". ", "!", "?", " ", ",", "，"):
        idx = cut.rfind(sep)
        if idx > len(cut) * 0.6:
            cut = cut[: idx + (1 if sep == ". " else 0)]
            break
    return cut.rstrip(" ,，、\n") + _ELLIPSIS
