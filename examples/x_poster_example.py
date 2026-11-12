#!/usr/bin/env python
"""X 发文适配器示例。

tg2x 通过 `publish.x.command` 调用任意可执行程序，把一条 JSON 从 stdin 丢进去。
你的 X 发文链路已经跑通，所以不用重写 —— 把这段逻辑替换成调用你现有函数即可。

输入 JSON 字段：
  text              最终要发的正文（已按 max_len 截断，含原文链接）
  text_en           英文译文（未截断，适合你自己重组）
  text_original     中文原文
  source_url        Telegram 原文链接
  source_channel    频道用户名
  source_message_id 消息 id
  media_files       已下载到本地的图片路径列表（可能为空）
  dedupe_key        channel:message_id，可用于二次去重

输出约定：成功时 stdout 打印 {"tweet_id": "..."}，退出码 0；失败退出码非 0。
"""
import json
import sys


def post_to_x(payload: dict) -> dict:
    """替换成你自己的发文实现，例如 tweepy / requests 调 X API。"""
    print("[example] 收到待发内容：", file=sys.stderr)
    print(payload["text"], file=sys.stderr)
    print(f"[example] 配图 {len(payload.get('media_files') or [])} 张", file=sys.stderr)

    # ---- 你的代码写在这里 ----
    # import tweepy
    # client = tweepy.Client(...)
    # media_ids = [upload(p) for p in payload["media_files"]]
    # resp = client.create_tweet(text=payload["text"], media_ids=media_ids)
    # return {"tweet_id": resp.data["id"]}
    raise NotImplementedError("请在这里接入你已跑通的 X 发文代码")


def main() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw)
    try:
        result = post_to_x(payload)
    except NotImplementedError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
