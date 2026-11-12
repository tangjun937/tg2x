"""SQLite 去重与状态存储 + 原文归档。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import TgPost, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    channel      TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    posted_at    TEXT,
    text_zh      TEXT,
    text_en      TEXT,
    media        TEXT,
    status       TEXT NOT NULL,   -- new / translated / published / failed / skipped
    tweet_id     TEXT,
    error        TEXT,
    created_at   TEXT,
    published_at TEXT,
    PRIMARY KEY (channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_status ON messages(status);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.archive_dir = self.data_dir / "archive"
        self.media_dir = self.data_dir / "media"
        for d in (self.data_dir, self.archive_dir, self.media_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "tg2x.sqlite3"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- meta ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )
        self.conn.commit()

    # ---------- messages ----------
    def seen(self, channel: str, message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE channel=? AND message_id=?",
            (channel, message_id),
        ).fetchone()
        return row is not None

    def last_seen_id(self, channel: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(message_id) AS m FROM messages WHERE channel=?", (channel,)
        ).fetchone()
        return int(row["m"] or 0)

    def record(self, post: TgPost, status: str = "new") -> None:
        self.conn.execute(
            """INSERT INTO messages(channel,message_id,posted_at,text_zh,media,status,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(channel,message_id) DO NOTHING""",
            (
                post.channel,
                post.message_id,
                post.posted_at,
                post.text,
                json.dumps(post.media_urls, ensure_ascii=False),
                status,
                now_iso(),
            ),
        )
        self.conn.commit()

    def mark(self, channel: str, message_id: int, status: str,
             text_en: str | None = None, tweet_id: str | None = None,
             error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE messages SET status=?,
                 text_en=COALESCE(?,text_en),
                 tweet_id=COALESCE(?,tweet_id),
                 error=?,
                 published_at=CASE WHEN ?='published' THEN ? ELSE published_at END
               WHERE channel=? AND message_id=?""",
            (status, text_en, tweet_id, error, status, now_iso(), channel, message_id),
        )
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM messages GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # ---------- 原文归档 ----------
    def archive(self, post: TgPost, extra: dict | None = None) -> Path:
        day = (post.posted_at or now_iso())[:10]
        d = self.archive_dir / day
        d.mkdir(parents=True, exist_ok=True)
        payload = post.to_dict()
        if extra:
            payload["pipeline"] = extra
        fp = d / f"{post.channel}_{post.message_id}.json"
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return fp

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
