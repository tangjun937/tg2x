"""抓取器统一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import TgPost


class Fetcher(ABC):
    """三种后端（web / telethon / bot）共用的最小接口。"""

    name = "base"

    @abstractmethod
    def fetch(self, limit: int = 20) -> list[TgPost]:
        """返回频道最近的若干条帖子，按 message_id 升序。"""

    def health(self) -> tuple[bool, str]:
        """连通性自检，返回 (是否可用, 说明)。"""
        try:
            posts = self.fetch(limit=1)
            if posts:
                return True, f"可读取，最新 message_id={posts[-1].message_id}"
            return False, "能连上但没取到帖子（频道无内容或权限不足）"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def close(self) -> None:
        pass
