"""配置加载：config.yaml + .env（环境变量优先）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载器，不引入额外依赖。已存在的环境变量不覆盖。"""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def root(self) -> Path:
        return ROOT

    def path_of(self, dotted: str, default: str = "") -> Path:
        p = Path(self.get(dotted, default) or default)
        return p if p.is_absolute() else (ROOT / p)

    def env(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)


def load(path: str | Path | None = None) -> Config:
    load_dotenv()
    cfg_path = Path(path) if path else (ROOT / "config.yaml")
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return Config(data, cfg_path)
