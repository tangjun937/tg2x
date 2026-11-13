"""广告 / 引流贴识别。

关注的频道里混着赌博推广、频道互推、加群引流这类帖子。
这些内容原样搬到 X 会被当成垃圾内容（甚至牵连账号权重），所以整条跳过。

判定三路：
  1) 结构特征：邀请链接、刷屏 emoji、纯链接堆
  2) 强广告词：命中任意一个即判定（赌博盘口、买粉、频道互推……）
  3) 弱广告词：累计命中 2 个才判定（加群、点击、注册……）

规则都写在 config.yaml 的 cleanup 段里，随时可改，不用动代码。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("tg2x")

URL_RE = re.compile(r"(?:https?://|t\.me/|telegram\.me/)\S+", re.IGNORECASE)
# t.me/xxx?start=autopromo... 这类带邀请参数的链接，基本只出现在引流贴里
INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/[^\s]*\?[^\s]*start=", re.IGNORECASE)
# ⚡️⚡️⚡️⚡️1️⃣1️⃣ 这种刷屏层
EMOJI_SPAM_RE = re.compile(r"[⚡🔥💥💰🎰🤑]{3,}")

DEFAULT_STRONG = [
    # 赌博 / 灰产
    "博彩", "赌博", "娱乐城", "棋牌", "彩票", "返水", "返佣", "首存", "上押", "下注",
    "投注", "包网", "刷流水", "招代理", "代理招商", "稳赚", "带单", "开奖",
    # 涨粉 / 互推 / 引流
    "买粉", "刷粉", "精选频道", "频道推荐", "收录频道", "互推", "广告位", "推广位",
    "推荐频道", "频道导航", "频道目录",
]

DEFAULT_WEAK = [
    "加群", "进群", "私聊", "邀请", "注册", "领取", "入口", "导航", "点击", "限时",
    "福利", "返利", "代理", "官方频道",
]


def ad_reason(text: str, strong: list[str] | None = None,
              weak: list[str] | None = None, min_weak: int = 2) -> str:
    """命中广告特征就返回原因字符串，否则返回空串。"""
    if not text or not text.strip():
        return ""

    if INVITE_RE.search(text):
        return "结构:邀请链接"

    urls = URL_RE.findall(text)
    plain = URL_RE.sub("", text)
    plain = re.sub(r"\s+", "", plain)
    if len(urls) >= 2 and len(plain) < 120:
        return f"结构:纯链接堆({len(urls)}个)"

    if EMOJI_SPAM_RE.search(text):
        return "结构:刷屏emoji"

    strong = DEFAULT_STRONG if strong is None else strong
    weak = DEFAULT_WEAK if weak is None else weak
    low = text.lower()

    for w in strong:
        if w and w.lower() in low:
            return f"强广告词:{w}"

    hits = [w for w in weak if w and w.lower() in low]
    if len(hits) >= min_weak:
        return f"弱广告词:{'、'.join(hits[:3])}"

    return ""
