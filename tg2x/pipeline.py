"""主流程：抓取 -> 去重 -> 归档 -> 翻译 -> 发布。"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
from pathlib import Path

import requests

from .adfilter import ad_reason
from .config import Config
from .dedup import is_duplicate
from .fetchers import build_fetcher
from .models import TgPost, TransPost, now_iso
from .notify import TgNotifier
from .publisher import Publisher
from .store import Store
from .translator import Translator
from .writer import NewsWriter

log = logging.getLogger("tg2x")


class Pipeline:
    def __init__(self, cfg: Config, dry_run: bool | None = None):
        self.cfg = cfg
        # TG 用户名大小写不敏感，统一小写，避免和解析结果对不上
        self.channel = (cfg.get("channel") or "").lstrip("@").lower()
        self.channels = self._parse_channels(cfg)
        self.store = Store(cfg.path_of("storage.data_dir", "data"))
        self.translator = Translator(cfg)
        self.writer = NewsWriter(cfg)
        self.publisher = Publisher(cfg)
        if dry_run is not None:
            self.publisher.dry_run = dry_run
        self._fetchers: dict[str, object] = {}
        self.notifier = TgNotifier(cfg)
        self._items: list[dict] = []
        self.max_per_cycle = int(cfg.get("fetch.max_per_cycle", 5) or 5)
        self.archive_media = bool(cfg.get("storage.archive_media", True))
        self.dedup_enabled = bool(cfg.get("cleanup.dedup_enabled", True))
        self.dedup_window = int(cfg.get("cleanup.dedup_window", 10) or 10)
        self.dedup_threshold = float(cfg.get("cleanup.dedup_threshold", 0.6) or 0.6)
        self.prune_keep = int(cfg.get("storage.prune_keep", 10) or 10)

    @staticmethod
    def _parse_channels(cfg: Config) -> list[dict]:
        chans = cfg.get("channels", None)
        out: list[dict] = []
        if chans and isinstance(chans, list):
            for c in chans:
                name = str(c.get("name", "")).lstrip("@").lower()
                if not name:
                    continue
                out.append({
                    "name": name,
                    "lang": (c.get("lang") or "zh").lower(),
                    "rewrite": bool(c.get("rewrite", False)),
                })
        if not out:
            out.append({"name": (cfg.get("channel") or "").lstrip("@").lower(),
                        "lang": "zh", "rewrite": False})
        return out

    # ---------- fetcher ----------
    def _fetcher(self, channel: str | None = None):
        key = (channel or self.channel).lower()
        if key not in self._fetchers:
            offset = int(self.store.get_meta("bot_offset", "0") or 0)
            self._fetchers[key] = build_fetcher(self.cfg, offset=offset, channel=key)
        return self._fetchers[key]

    # ---------- 一轮 ----------
    def cycle(self, publish: bool = True) -> dict:
        result = {"fetched": 0, "new": 0, "published": 0, "failed": 0, "skipped": 0}
        self._items = []

        # 1. 收集所有频道的新帖
        candidates: list[tuple[dict, TgPost]] = []
        for ch in self.channels:
            for p in self._gather_channel(ch, result):
                candidates.append((ch, p))

        # 2. 影响排序（DeepSeek），只发影响大的前 N 篇
        ranked = candidates
        if len(candidates) > 1:
            ch_map = {id(p): ch for ch, p in candidates}
            posts = [p for _, p in candidates]
            ordered = self.writer.rank(posts)
            if len(ordered) == len(posts):
                ranked = [(ch_map[id(p)], p) for p in ordered]
        if len(ranked) > self.max_per_cycle:
            log.info("本轮 %d 篇新帖，按影响排序后只发前 %d 篇",
                     len(ranked), self.max_per_cycle)
            ranked = ranked[: self.max_per_cycle]

        # 3. 逐篇处理（第 1 篇立即发，第 2 篇起排到 60 分钟后）
        for idx, (ch, post) in enumerate(ranked):
            result["new"] += 1
            status = "failed"
            try:
                status = self._handle(post, ch, publish=publish, position=idx)
            except Exception as e:  # noqa: BLE001
                log.exception("处理失败 %s: %s", post.key, e)
                self.store.mark(post.channel, post.message_id, "failed", error=str(e)[:500])
                self._items.append({"key": post.key, "text": "", "error": str(e)[:300]})
            result[status] += 1

        # 每轮结束清理：每频道只保留最近 N 条，防止库无限增长
        if self.prune_keep > 0:
            removed = self.store.prune(self.prune_keep)
            if removed:
                log.info("清理去重库：删除 %d 条旧记录（每频道保留最近 %d 条）",
                         removed, self.prune_keep)
        self.notifier.report(result, self._items)
        return result

    def _gather_channel(self, ch: dict, result: dict) -> list[TgPost]:
        """抓取某频道并返回本轮新帖（首次运行只建基线，返回空）。"""
        fetcher = self._fetcher(ch["name"])
        posts = fetcher.fetch(limit=30)
        result["fetched"] += len(posts)

        # 中文原文只导出主频道，供本机国内分发器拉取（英文频道不用导）
        if ch["name"] == self.channel:
            self._export_cn_feed(posts)

        if getattr(fetcher, "offset", None):
            self.store.set_meta("bot_offset", str(fetcher.offset))

        boot_key = f"boot_done:{ch['name']}"
        first_run = self.store.get_meta(boot_key) != "1"
        if first_run and not self.cfg.get("publish.existing_on_boot", False):
            for p in posts:
                self.store.record(p, status="skipped")
                self.store.archive(p, {"reason": "boot-baseline"})
            self.store.set_meta(boot_key, "1")
            result["skipped"] += len(posts)
            log.info("频道 @%s 首次启动：已把 %d 条历史帖标记为基线，不发布",
                     ch["name"], len(posts))
            return []

        fresh = [p for p in posts if not self.store.seen(p.channel, p.message_id)]
        fresh.sort(key=lambda p: p.message_id, reverse=True)
        return fresh

    def _handle(self, post: TgPost, ch: dict, publish: bool = True, position: int = 0) -> str:
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

        text_en = ""
        if post.text.strip():
            # DeepSeek 写作（洗稿 + 互动形式 + 写作总册）；失败退回原翻译/清洗
            text_en = self.writer.write(post.text)
            if not text_en:
                text_en = (self.translator._clean_en(post.text) if ch["lang"] == "en"
                           else self.translator.translate(post.text))
        # 统一品牌前缀（如 "FinBrief Alert: "）
        prefix = (self.cfg.get("translate.prefix", "") or "").strip()
        if prefix and text_en and not text_en.startswith(prefix):
            text_en = f"{prefix}{text_en}"

        # 关键词去重：与最近已发内容高度重复则跳过（纯规则，不用 AI）
        if self.dedup_enabled and text_en and is_duplicate(
                text_en, self.store.recent_published(self.dedup_window),
                self.dedup_threshold):
            log.info("跳过 [%s]：与最近已发内容重复", post.key)
            self.store.record(post, "skipped")
            self.store.archive(post, {"reason": "duplicate-keyword"})
            return "skipped"

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

    # ---------- 中文原文导出 ----------
    def _export_cn_feed(self, posts: list[TgPost]) -> None:
        """把抓到的中文原文导出到 data/cn_feed.json，供本机国内分发器拉取。"""
        try:
            feed = {
                "channel": self.channel,
                "updated_at": now_iso(),
                "posts": [
                    {"message_id": p.message_id, "text": p.text,
                     "posted_at": p.posted_at, "url": p.url}
                    for p in posts if p.text.strip()
                ],
            }
            fp = self.store.data_dir / "cn_feed.json"
            fp.write_text(json.dumps(feed, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("导出中文原文失败：%s", e)

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
        ch = self.channels[0]
        posts = self._fetcher(ch["name"]).fetch(limit=limit)
        out = {"fetched": len(posts), "new": 0, "published": 0, "failed": 0}
        for idx, p in enumerate(posts):
            if self.store.seen(p.channel, p.message_id):
                continue
            out["new"] += 1
            try:
                status = self._handle(p, ch, publish=publish, position=idx)
                out["published" if status == "published" else "failed"] += 1
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                log.error("回填失败 %s: %s", p.key, e)
        return out

    def loop(self, interval: int | None = None) -> None:
        interval = interval or int(self.cfg.get("fetch.interval_seconds", 30) or 30)
        names = ", ".join(c["name"] for c in self.channels)
        log.info("开始轮询 @%s（后端=%s，间隔=%ss）", names,
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
        for f in self._fetchers.values():
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
