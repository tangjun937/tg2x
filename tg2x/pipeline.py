"""主流程：抓取 -> 去重 -> 归档 -> 翻译 -> 发布。"""
from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path

import requests

from .adfilter import ad_reason
from .config import Config
from .fetchers import build_fetcher
from .models import TgPost, TransPost, now_iso
from .notify import TgNotifier
from .publisher import Publisher
from .store import Store
from .translator import Translator

log = logging.getLogger("tg2x")


class Pipeline:
    def __init__(self, cfg: Config, dry_run: bool | None = None):
        self.cfg = cfg
        # TG 用户名大小写不敏感，统一小写，避免和解析结果对不上
        self.channel = (cfg.get("channel") or "").lstrip("@").lower()
        self.store = Store(cfg.path_of("storage.data_dir", "data"))
        self.translator = Translator(cfg)
        self.publisher = Publisher(cfg)
        if dry_run is not None:
            self.publisher.dry_run = dry_run
        self.fetcher = None
        self.notifier = TgNotifier(cfg)
        self._items: list[dict] = []
        self.max_per_cycle = int(cfg.get("fetch.max_per_cycle", 5) or 5)
        self.archive_media = bool(cfg.get("storage.archive_media", True))

    # ---------- fetcher ----------
    def _fetcher(self):
        if self.fetcher is None:
            offset = int(self.store.get_meta("bot_offset", "0") or 0)
            self.fetcher = build_fetcher(self.cfg, offset=offset)
        return self.fetcher

    # ---------- 一轮 ----------
    def cycle(self, publish: bool = True) -> dict:
        result = {"fetched": 0, "new": 0, "published": 0, "failed": 0, "skipped": 0}
        self._items = []
        fetcher = self._fetcher()
        posts = fetcher.fetch(limit=30)
        result["fetched"] = len(posts)

        if getattr(fetcher, "offset", None):
            self.store.set_meta("bot_offset", str(fetcher.offset))

        boot_key = f"boot_done:{self.channel}"
        first_run = self.store.get_meta(boot_key) != "1"
        if first_run and not self.cfg.get("publish.existing_on_boot", False):
            for p in posts:
                self.store.record(p, status="skipped")
                self.store.archive(p, {"reason": "boot-baseline"})
            self.store.set_meta(boot_key, "1")
            result["skipped"] = len(posts)
            log.info("首次启动：已把 %d 条历史帖标记为基线，不发布", len(posts))
            return result

        fresh = [p for p in posts if not self.store.seen(p.channel, p.message_id)]
        # 从新到旧：只搬最新的，不追旧帖（陈旧资讯发出去没意义）
        fresh.sort(key=lambda p: p.message_id, reverse=True)
        if len(fresh) > self.max_per_cycle:
            log.info("本轮新帖 %d 条，超过上限 %d，只处理最新的 %d 条",
                     len(fresh), self.max_per_cycle, self.max_per_cycle)
            fresh = fresh[: self.max_per_cycle]

        for idx, post in enumerate(fresh):
            result["new"] += 1
            status = "failed"
            try:
                status = self._handle(post, publish=publish, position=idx)
            except Exception as e:  # noqa: BLE001
                log.exception("处理失败 %s: %s", post.key, e)
                self.store.mark(post.channel, post.message_id, "failed", error=str(e)[:500])
                self._items.append({"key": post.key, "text": "", "error": str(e)[:300]})
            result[status] += 1

        self.notifier.report(result, self._items)
        return result

    def _handle(self, post: TgPost, publish: bool = True, position: int = 0) -> str:
        """返回 published / skipped / failed 三种状态之一。"""
        if not post.text.strip() and not post.media_urls:
            self.store.record(post, "skipped")
            return "skipped"

        reason = self._skip_reason(post.text)
        if reason:
            log.info("跳过 [%s]：%s", post.key, reason)
            self.store.record(post, "skipped")
            self.store.archive(post, {"reason": reason})
            return "skipped"

        self.store.record(post, "new")
        self._fetch_media(post)

        text_en = self.translator.translate(post.text) if post.text.strip() else ""
        item = TransPost(post=post, text_en=text_en)
        item.final_text = self.publisher.build_text(item)
        self.store.mark(post.channel, post.message_id, "translated", text_en=text_en)
        self.store.archive(post, {"text_en": text_en, "final_text": item.final_text,
                                  "archived_at": now_iso()})
        log.info("译文 [%s] %s", post.key, text_en.replace("\n", " ")[:120])

        if not publish:
            self.store.mark(post.channel, post.message_id, "translated")
            return "skipped"

        res = self.publisher.publish(item, position=position)
        if res.ok:
            if self.publisher.dry_run:
                # dry run 不写去重库，否则这条会被当成已发，真跑时反而漏掉
                self.store.mark(post.channel, post.message_id, "translated")
                log.info("DRY-RUN 生成完毕 [%s]（未入库，下一轮仍会真发）", post.key)
                self._items.append({"key": post.key, "text": item.final_text, "error": None})
                return "published"
            self.store.mark(post.channel, post.message_id, "published",
                            tweet_id=res.tweet_id, error=None)
            log.info("已发 X [%s] tweet=%s", post.key, res.tweet_id or "?")
            self._items.append({"key": post.key, "text": item.final_text, "error": None})
            return "published"

        self.store.mark(post.channel, post.message_id, "failed", error=res.detail)
        log.error("发 X 失败 [%s] %s", post.key, res.detail)
        self._items.append({"key": post.key, "text": item.final_text, "error": res.detail})
        return "failed"

    # ---------- 跳过判定 ----------
    def _skip_reason(self, text: str) -> str:
        """黑名单词或广告特征命中就返回原因，否则返回空串。"""
        hit = self._blocked_by(text)
        if hit:
            return f"屏蔽词:{hit}"
        return ad_reason(
            text,
            strong=self.cfg.get("cleanup.ad_keywords", None),
            weak=self.cfg.get("cleanup.ad_weak_keywords", None),
            min_weak=int(self.cfg.get("cleanup.ad_weak_threshold", 2) or 2),
        )

    # ---------- 关键词屏蔽 ----------
    def _blocked_by(self, text: str) -> str:
        """命中 cleanup.block_keywords 里的任一词就返回该词，否则返回空串。"""
        words = self.cfg.get("cleanup.block_keywords", None) or []
        if not words or not text:
            return ""
        low = text.lower()
        for w in words:
            if w and str(w).lower() in low:
                return str(w)
        return ""

    # ---------- 图片落地 ----------
    def _fetch_media(self, post: TgPost) -> None:
        if not self.archive_media or not post.media_urls:
            return
        files: list[str] = []
        for i, url in enumerate(post.media_urls[:4], 1):
            if not url.startswith("http"):
                continue  # tg-media:// 等需 telethon 下载，暂跳过
            try:
                r = requests.get(url, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                ext = mimetypes.guess_extension(
                    (r.headers.get("Content-Type") or "").split(";")[0]
                ) or ".jpg"
                fp = self.store.media_dir / f"{post.channel}_{post.message_id}_{i}{ext}"
                fp.write_bytes(r.content)
                files.append(str(fp))
            except Exception as e:  # noqa: BLE001
                log.warning("图片下载失败 %s: %s", url[:80], e)
        post.media_files = files

    # ---------- 回填 ----------
    def backfill(self, limit: int = 20, publish: bool = False) -> dict:
        posts = self._fetcher().fetch(limit=limit)
        out = {"fetched": len(posts), "new": 0, "published": 0, "failed": 0}
        for idx, p in enumerate(posts):
            if self.store.seen(p.channel, p.message_id):
                continue
            out["new"] += 1
            try:
                status = self._handle(p, publish=publish, position=idx)
                out["published" if status == "published" else "failed"] += 1
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                log.error("回填失败 %s: %s", p.key, e)
        return out

    def loop(self, interval: int | None = None) -> None:
        interval = interval or int(self.cfg.get("fetch.interval_seconds", 30) or 30)
        log.info("开始轮询 @%s（后端=%s，间隔=%ss）", self.channel,
                 self.cfg.get("fetch.backend"), interval)
        while True:
            try:
                r = self.cycle()
                log.info("本轮：新帖 %d，发布 %d，失败 %d",
                         r["new"], r["published"], r["failed"])
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("抓取异常：%s", e)
            time.sleep(interval)

    def close(self) -> None:
        if self.fetcher is not None:
            self.fetcher.close()
