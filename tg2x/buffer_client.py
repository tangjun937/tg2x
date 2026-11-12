"""Buffer GraphQL API 客户端。

Buffer 2026 版公开 API：单一端点 https://api.buffer.com，Bearer 个人 API key，
全部操作用 GraphQL。

关键限制：**不支持文件上传**，图片必须是公网可访问的 URL。
好在 Telegram 的图片 CDN（cdn*.telesco.pe）本身就是公开链接，可以直接传过去。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

ENDPOINT = "https://api.buffer.com"

Q_ORGS = """
query GetOrganizations {
  account {
    organizations { id name }
  }
}
"""

Q_CHANNELS = """
query GetChannels($organizationId: String!) {
  channels(input: { organizationId: $organizationId }) {
    id
    name
    displayName
    service
    serviceId
    timezone
    isDisconnected
  }
}
"""

M_CREATE = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id text dueAt status }
    }
    ... on MutationError {
      message
    }
  }
}
"""


class BufferError(RuntimeError):
    pass


class BufferClient:
    def __init__(self, token: str, endpoint: str = ENDPOINT, timeout: int = 30):
        if not token:
            raise BufferError("缺少 Buffer API key（.env 里的 BUFFER_TOKEN）")
        self.token = token
        self.endpoint = endpoint
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    # ---------- 底层 ----------
    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        r = self.session.post(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        if r.status_code == 401:
            raise BufferError("Buffer 认证失败（401）：API key 无效或已撤销")
        if r.status_code >= 400:
            raise BufferError(f"Buffer HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError as e:
            raise BufferError(f"Buffer 返回非 JSON：{r.text[:200]}") from e
        if data.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise BufferError(f"Buffer GraphQL 错误：{msg}")
        return data.get("data") or {}

    # ---------- 查询 ----------
    def organizations(self) -> list[dict]:
        data = self.gql(Q_ORGS)
        return ((data.get("account") or {}).get("organizations")) or []

    def channels(self, organization_id: str) -> list[dict]:
        data = self.gql(Q_CHANNELS, {"organizationId": organization_id})
        return data.get("channels") or []

    def find_x_channel(self) -> tuple[str, dict]:
        """返回 (organizationId, channel)。有多个 X 频道时抛错，让用户自己指定。"""
        orgs = self.organizations()
        if not orgs:
            raise BufferError("这个 Buffer 账号下没有组织（organization）")

        candidates: list[tuple[str, dict]] = []
        for org in orgs:
            for ch in self.channels(org["id"]):
                svc = (ch.get("service") or "").lower()
                if svc in ("twitter", "x"):
                    candidates.append((org["id"], ch))
        if not candidates:
            raise BufferError("Buffer 里没有连接 X/Twitter 频道，请先在 Buffer 后台连上")
        if len(candidates) == 1:
            return candidates[0]

        names = "、".join(f"{c['name']}({c['id']})" for _, c in candidates)
        raise BufferError(
            f"Buffer 下有 {len(candidates)} 个 X 频道，请在 config.yaml 里指定 "
            f"publish.buffer.channel_id：{names}"
        )

    # ---------- 发文 ----------
    def create_post(
        self,
        channel_id: str,
        text: str,
        assets: list[dict] | None = None,
        mode: str = "shareNow",
        due_at: str | None = None,
    ) -> dict:
        """mode: shareNow（立即）/ addToQueue（下一个空档）/ customScheduled（指定时间）"""
        inp: dict[str, Any] = {
            "text": text,
            "channelId": channel_id,
            "schedulingType": "automatic",
            "mode": mode,
        }
        if due_at:
            inp["dueAt"] = due_at
        if assets:
            inp["assets"] = assets
        data = self.gql(M_CREATE, {"input": inp})
        res = data.get("createPost") or {}
        if "message" in res and "post" not in res:
            raise BufferError(f"Buffer 拒绝发布：{res['message']}")
        post = res.get("post") or {}
        if not post.get("id"):
            raise BufferError(f"Buffer 返回异常：{json.dumps(res, ensure_ascii=False)[:300]}")
        return post


def image_assets(urls: list[str], limit: int = 4) -> list[dict]:
    """把公网图片 URL 列表转成 Buffer 的 assets 结构。"""
    out: list[dict] = []
    for u in urls:
        if not u or not u.startswith("http"):
            continue
        out.append({"image": {"url": u}})
        if len(out) >= limit:
            break
    return out


def load_channel_cache(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_channel_cache(path: Path, org_id: str, channel: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps({"organizationId": org_id, "channelId": channel.get("id"),
                    "channelName": channel.get("name"),
                    "service": channel.get("service")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
