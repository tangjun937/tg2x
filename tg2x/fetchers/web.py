"""web 后端：抓 https://t.me/s/<channel> 公开预览页。

优点：零凭证、不用登录、不会被封号；30 秒一轮完全够用。
限制：只能读「公开频道」（有 @username 且未关闭网页预览）；拿不到私有频道。
"""
from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from ..models import TgPost, parse_tg_time
from .base import Fetcher

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

URL_RE = re.compile(r"url\(['\"]?(.*?)['\"]?\)")


class WebFetcher(Fetcher):
    name = "web"

    def __init__(self, channel: str, timeout: int = 20, user_agent: str = UA):
        self.channel = channel.lstrip("@")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    @property
    def url(self) -> str:
        return f"https://t.me/s/{self.channel}"

    def fetch(self, limit: int = 20) -> list[TgPost]:
        resp = self.session.get(self.url, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}：{self.url}")
        posts = self.parse(resp.text)
        if not posts:
            # 页面能打开但没有消息，通常意味着：频道不存在 / 已关闭网页预览 / 是私有频道
            raise RuntimeError(
                f"{self.url} 未解析到任何消息：频道可能不存在、非公开，或关闭了网页预览"
            )
        return posts[-limit:]

    # ---------- 解析 ----------
    def parse(self, html: str) -> list[TgPost]:
        soup = BeautifulSoup(html, "lxml")
        out: list[TgPost] = []
        for wrap in soup.select("div.tgme_widget_message"):
            data_post = wrap.get("data-post") or ""
            if "/" not in data_post:
                continue
            ch, _, mid = data_post.partition("/")
            try:
                message_id = int(mid)
            except ValueError:
                continue

            text_el = wrap.select_one(".tgme_widget_message_text")
            text = ""
            if text_el is not None:
                # <br> 转成换行；去掉转发来源之类的杂项
                for br in text_el.find_all("br"):
                    br.replace_with("\n")
                text = text_el.get_text("", strip=False).strip()

            time_el = wrap.select_one("time[datetime]")
            posted_at = parse_tg_time(time_el["datetime"]) if time_el else ""

            media: list[str] = []
            for el in wrap.select(".tgme_widget_message_photo_wrap"):
                m = URL_RE.search(el.get("style") or "")
                if m:
                    media.append(m.group(1))
            for el in wrap.select("a.tgme_widget_message_video"):
                href = el.get("href")
                if href:
                    media.append(href)

            out.append(
                TgPost(
                    channel=ch,
                    message_id=message_id,
                    text=text,
                    posted_at=posted_at,
                    url=f"https://t.me/{ch}/{message_id}",
                    media_urls=media,
                    source=self.name,
                )
            )
        out.sort(key=lambda p: p.message_id)
        return out
