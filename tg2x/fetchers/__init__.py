"""三种抓取后端的工厂。"""
from __future__ import annotations

import os

from ..config import Config
from .base import Fetcher
from .bot_fetcher import BotFetcher
from .telethon_fetcher import TelethonFetcher
from .web import WebFetcher

__all__ = ["Fetcher", "build_fetcher", "WebFetcher", "TelethonFetcher", "BotFetcher"]


def build_fetcher(cfg: Config, offset: int = 0) -> Fetcher:
    backend = (cfg.get("fetch.backend") or "web").lower()
    channel = cfg.get("channel") or ""
    timeout = int(cfg.get("fetch.request_timeout", 20) or 20)

    if backend == "web":
        return WebFetcher(channel, timeout=timeout,
                          user_agent=cfg.get("fetch.user_agent", "") or
                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

    if backend == "telethon":
        api_id = os.environ.get("TG_API_ID", "")
        api_hash = os.environ.get("TG_API_HASH", "")
        if not api_id or not api_hash:
            raise RuntimeError("telethon 模式需要在 .env 里配置 TG_API_ID / TG_API_HASH")
        session = str(cfg.path_of("storage.data_dir", "data") / "tg2x")
        return TelethonFetcher(
            channel=channel,
            api_id=int(api_id),
            api_hash=api_hash,
            session_path=session,
            phone=os.environ.get("TG_PHONE", ""),
        ).connect()

    if backend == "bot":
        token = os.environ.get("TG_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("bot 模式需要在 .env 里配置 TG_BOT_TOKEN")
        return BotFetcher(channel=channel, token=token, offset=offset, timeout=timeout)

    raise RuntimeError(f"未知 fetch.backend: {backend}（可选 web / telethon / bot）")
