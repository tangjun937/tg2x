#!/usr/bin/env python
"""冒烟测试：不碰网络、不发真帖，验证 store / 翻译清洗 / 发布适配器协议。

运行： python tests/smoke_test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tg2x import config as cfgmod  # noqa: E402
from tg2x.models import TgPost, TransPost  # noqa: E402
from tg2x.publisher import Publisher, _clip  # noqa: E402
from tg2x.store import Store  # noqa: E402
from tg2x.translator import Translator  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def test_store(tmp: Path) -> None:
    print("store / 去重")
    s = Store(tmp / "data")
    p = TgPost(channel="ch", message_id=1, text="hi")
    check("初始未见过", not s.seen("ch", 1))
    s.record(p, "new")
    check("记录后可见", s.seen("ch", 1))
    s.record(p, "new")  # 重复写入不应报错
    check("last_seen_id", s.last_seen_id("ch") == 1)
    s.mark("ch", 1, "published", text_en="hi", tweet_id="t1")
    check("stats", s.stats().get("published") == 1, str(s.stats()))
    fp = s.archive(p, {"x": 1})
    archived = json.loads(fp.read_text("utf-8")) if fp.exists() else {}
    check("归档文件落盘", fp.exists() and archived.get("message_id") == 1)
    s.close()


def test_clean(tmp: Path) -> None:
    print("原文清洗 / 话题标签")
    cfg = cfgmod.load()
    t = Translator(cfg)
    raw = ("#网友投稿：昨晚金边一仓库起火。\n\n关注全球大事件频道➡️ @tgssse   投稿：@czx55")
    cleaned = t._clean(raw)
    check("推广尾巴被去掉", "关注全球" not in cleaned and "@czx55" not in cleaned, repr(cleaned))
    check("正文保留", "金边" in cleaned)
    check("中文标注不被当标签吞掉", "网友投稿" in cleaned)

    # 这个频道的尾巴样式：emoji 开头 + t.me 链接 + 投稿
    raw2 = "\n".join([
        "#英国新闻",
        "惊爆！美军军官英国被捕",
        "",
        "正文段落。",
        "",
        "📢  订阅全球大事件曝光",
        "🔗 t.me/ymish3333",
        "😘 投稿爆料：@DJNB009",
    ])
    c2 = t._clean(raw2)
    check("emoji 推广行被剥掉", "订阅" not in c2 and "DJNB009" not in c2, repr(c2))
    check("正文完整保留", "美军军官英国被捕" in c2 and "正文段落" in c2)
    check("正文里的关键词不误删",
          "频道" in t._clean("警方通报称该频道传播谣言，已立案。"))


def test_clip() -> None:
    print("长度截断")
    long = "This is a sentence with many words " * 20
    out = _clip(long, 100)
    check("不超过上限", len(out) <= 100, f"{len(out)}")
    check("带省略号", out.endswith("…"))
    check("不切半个单词", "  " not in out and not out.endswith(" …"))


def test_publisher_command(tmp: Path) -> None:
    print("发布适配器（command 协议）")
    fake = tmp / "fake_poster.py"
    record = tmp / "received.json"
    fake.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        f"open(r'{record}','w',encoding='utf-8').write(json.dumps(p,ensure_ascii=False))\n"
        "print(json.dumps({'tweet_id':'999'}))\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load()
    cfg._data["publish"]["target"] = "command"
    cfg._data["publish"]["command"]["command"] = f'python "{fake}"'
    cfg._data["publish"]["dry_run"] = False

    pub = Publisher(cfg)
    post = TgPost(channel="ch", message_id=7, text="原文",
                  url="https://t.me/ch/7", media_files=["a.jpg"])
    item = TransPost(post=post, text_en="English body")
    res = pub.publish(item)
    check("返回成功", res.ok, res.detail)
    check("拿到 tweet_id", res.tweet_id == "999", res.tweet_id)
    payload = json.loads(record.read_text("utf-8"))
    check("stdin 传了正文", payload["text"].startswith("English body"))
    check("带原文链接", payload["source_url"] == "https://t.me/ch/7")
    check("带配图路径", payload["media_files"] == ["a.jpg"])
    check("带去重键", payload["dedupe_key"] == "ch:7")


def test_publisher_fail(tmp: Path) -> None:
    print("发布失败要能上报")
    bad = tmp / "bad_poster.py"
    bad.write_text("import sys; sys.exit(4)\n", encoding="utf-8")
    cfg = cfgmod.load()
    cfg._data["publish"]["target"] = "command"
    cfg._data["publish"]["command"]["command"] = f'python "{bad}"'
    cfg._data["publish"]["dry_run"] = False
    pub = Publisher(cfg)
    post = TgPost(channel="ch", message_id=8, text="x", url="https://t.me/ch/8")
    res = pub.publish(TransPost(post=post, text_en="body"))
    check("失败被捕获", not res.ok, res.detail)


def test_buffer() -> None:
    print("发布适配器（Buffer GraphQL）")

    class FakeClient:
        def __init__(self):
            self.calls = []

        def create_post(self, channel_id, text, assets=None, mode="shareNow", due_at=None):
            self.calls.append({"channel_id": channel_id, "text": text,
                               "assets": assets or [], "mode": mode, "due_at": due_at})
            return {"id": "buf_123", "dueAt": due_at or "2026-11-12T10:00:00.000Z"}

    cfg = cfgmod.load()
    cfg._data["publish"]["target"] = "buffer"
    cfg._data["publish"]["dry_run"] = False
    cfg._data["publish"]["buffer"]["channel_id"] = "chan_abc"

    pub = Publisher(cfg)
    fake = FakeClient()
    pub._buf = fake
    post = TgPost(
        channel="YMISH3333", message_id=23868, text="原文",
        url="https://t.me/YMISH3333/23868",
        media_urls=["https://cdn5.telesco.pe/a.jpg", "ftp://bad", "https://cdn5.telesco.pe/b.jpg"],
    )
    res = pub.publish(TransPost(post=post, text_en="UK police arrest US officer"))
    check("返回成功", res.ok, res.detail)
    check("拿到 buffer post id", res.tweet_id == "buf_123", res.tweet_id)
    call = fake.calls[0]
    check("channel 正确", call["channel_id"] == "chan_abc")
    check("正文含链接", "https://t.me/YMISH3333/23868" in call["text"])
    check("图片转成 assets", len(call["assets"]) == 2, str(call["assets"]))
    check("assets 结构正确",
          call["assets"][0] == {"image": {"url": "https://cdn5.telesco.pe/a.jpg"}},
          str(call["assets"][0]))
    check("非 http 链接被剔除", all("ftp" not in str(a) for a in call["assets"]))
    check("默认立即发", call["mode"] == "shareNow")


def test_buffer_max_assets() -> None:
    print("Buffer 配图数量上限")
    from tg2x.buffer_client import image_assets
    urls = [f"https://x/{i}.jpg" for i in range(10)]
    check("默认最多 4 张", len(image_assets(urls)) == 4)
    check("可配置", len(image_assets(urls, limit=2)) == 2)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_store(tmp)
        test_clean(tmp)
        test_clip()
        test_publisher_command(tmp)
        test_publisher_fail(tmp)
    test_buffer()
    test_buffer_max_assets()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
