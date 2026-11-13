#!/usr/bin/env python
"""tg2x 命令行入口。

用法：
  python run.py doctor              # 环境 / 连通性 / 翻译 / 发布通道 全量自检
  python run.py probe --limit 3     # 抓最新 3 条，打印原文 + 英文译文（不发布）
  python run.py once --dry-run      # 跑一轮，只打印不真发
  python run.py once                # 跑一轮，真发
  python run.py loop                # 常驻轮询
  python run.py backfill --limit 20 # 回填历史帖（默认不发，加 --publish 才发）
  python run.py stats               # 看数据库统计
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tg2x import config as cfgmod  # noqa: E402
from tg2x.fetchers import build_fetcher  # noqa: E402
from tg2x.pipeline import Pipeline  # noqa: E402
from tg2x.translator import Translator  # noqa: E402


def setup_log(verbose: bool = True) -> None:
    data = Path(__file__).resolve().parent / "data"
    data.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    fh = logging.FileHandler(data / "tg2x.log", encoding="utf-8")
    handlers.append(fh)
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def cmd_doctor(cfg: cfgmod.Config) -> int:
    print("=" * 66)
    print(" tg2x 自检")
    print("=" * 66)
    print(f" 配置文件        : {cfg.path}")
    print(f" 目标频道        : @{cfg.get('channel')}")
    print(f" 抓取后端        : {cfg.get('fetch.backend')}")

    print("\n[1] Telegram 连通性")
    import requests
    for url in ("https://t.me", "https://api.telegram.org"):
        try:
            code = requests.get(url, timeout=10, allow_redirects=False).status_code
            print(f"    {url:<28} 可访问 (HTTP {code})")
        except Exception as e:  # noqa: BLE001
            print(f"    {url:<28} 失败: {e}")

    print("\n[2] 频道读取")
    ok = False
    try:
        f = build_fetcher(cfg)
        ok, detail = f.health()
        print(f"    {detail}")
        f.close()
    except Exception as e:  # noqa: BLE001
        print(f"    失败: {e}")
    if not ok:
        print("    → 检查频道用户名是否正确、是否为公开频道（t.me/<用户名> 能打开）")

    print("\n[3] 翻译通道")
    try:
        t = Translator(cfg)
        sample = "泰国警方今早搜查了曼谷另一处住宅，现场发现大量文件。"
        print(f"    provider = {t.provider}")
        print(f"    原文: {sample}")
        print(f"    译文: {t.translate(sample)}")
    except Exception as e:  # noqa: BLE001
        print(f"    失败: {e}")

    print("\n[4] 发布通道")
    target = cfg.get("publish.target") or "dry_run"
    effective = "ON（不会真发）" if (cfg.get("publish.dry_run") or target == "dry_run") \
        else "OFF（会真发出去）"
    print(f"    target = {target}   dry_run = {effective}")

    if target == "buffer":
        token = cfg.env(cfg.get("publish.buffer.token_env", "BUFFER_TOKEN"))
        print(f"    BUFFER_TOKEN: {'已配置' if token else '缺失 → 去 Buffer 后台 Settings → API 生成'}")
        ch = (cfg.get("publish.buffer.channel_id") or "").strip()
        cache = cfg.path_of("storage.data_dir", "data") / "buffer_channel.json"
        if ch:
            print(f"    channel_id = {ch}（配置指定）")
        elif cache.exists():
            import json as _json
            c = _json.loads(cache.read_text(encoding="utf-8"))
            print(f"    channel_id = {c.get('channelId')}（缓存，{c.get('channelName')}）")
        else:
            print("    channel_id = 未指定，首次发文时自动查找（或跑 buffer-setup）")
        if token:
            try:
                from tg2x.buffer_client import BufferClient
                orgs = BufferClient(token).organizations()
                print(f"    Buffer 组织: {', '.join(o['name'] for o in orgs) or '无'}")
            except Exception as e:  # noqa: BLE001
                print(f"    Buffer 查询失败: {e}")
    elif target == "command":
        cmd = cfg.get("publish.command.command") or ""
        print(f"    command = {cmd}")
        if cmd:
            p = cmd.split('"')[1] if '"' in cmd else cmd.split()[-1]
            print(f"    脚本存在: {Path(p).exists()}")
    elif target == "http":
        print(f"    url = {cfg.get('publish.http.url')}")

    print("\n" + "=" * 66)
    return 0 if ok else 1


def cmd_bot_id(cfg: cfgmod.Config) -> int:
    """找出你自己的 chat_id：先给 bot 发一句 /start，再跑这个。"""
    from tg2x.notify import discover_chats

    token = cfg.env(cfg.get("notify.bot_token_env", "TG_BOT_TOKEN"))
    if not token:
        print("缺少 TG_BOT_TOKEN：.env 里填上 @BotFather 给的 token")
        return 1
    try:
        chats = discover_chats(token)
    except Exception as e:  # noqa: BLE001
        print(f"查询失败：{e}")
        return 1
    if not chats:
        print("没找到任何会话。请先在 Telegram 里给 bot 发一句 /start，然后重跑本命令。")
        return 1
    print("找到这些会话，把 id 填进 .env 的 TG_CHAT_ID：")
    for c in chats:
        print(f"  {c['id']:<16} {c['type']:<10} {c['name']}")
    return 0


def cmd_notify_test(cfg: cfgmod.Config) -> int:
    from tg2x.notify import TgNotifier

    n = TgNotifier(cfg)
    if not n.enabled:
        print("通知未启用：检查 .env 里的 TG_BOT_TOKEN / TG_CHAT_ID，以及 config.yaml 的 notify.enabled")
        return 1
    ok = n.send("✅ tg2x 通知通道已接通。\n可以开始接收发布回执了。")
    print("已发送" if ok else "发送失败，检查 chat_id 是否正确")
    return 0 if ok else 1


def cmd_buffer_setup(cfg: cfgmod.Config, org_id: str = "", channel_id: str = "") -> int:
    """列出 Buffer 的组织和频道，并把选定的 X 频道写进缓存。"""
    import json as _json

    from tg2x.buffer_client import BufferClient, load_channel_cache, save_channel_cache

    token = cfg.env(cfg.get("publish.buffer.token_env", "BUFFER_TOKEN"))
    if not token:
        print("缺少 BUFFER_TOKEN。到 Buffer 后台 Settings → API 生成个人 API key，"
              "写进 .env 的 BUFFER_TOKEN= 后重试")
        return 1

    try:
        client = BufferClient(token)
        orgs = client.organizations()
    except Exception as e:  # noqa: BLE001
        print(f"连接 Buffer 失败: {e}")
        return 1

    print(f"找到 {len(orgs)} 个组织：")
    rows: list[tuple[str, dict]] = []
    for org in orgs:
        if org_id and org["id"] != org_id:
            continue
        print(f"\n  组织 {org['name']}  ({org['id']})")
        try:
            chans = client.channels(org["id"])
        except Exception as e:  # noqa: BLE001
            print(f"    取频道失败: {e}")
            continue
        if not chans:
            print("    （没有连接的频道）")
        for ch in chans:
            print(f"    - {ch.get('name'):<24} service={ch.get('service'):<10} id={ch.get('id')}")
            rows.append((org["id"], ch))

    if channel_id:
        hit = next(((o, c) for o, c in rows if c.get("id") == channel_id), None)
        if not hit:
            print(f"\n没找到 channel_id={channel_id}")
            return 1
        cache = cfg.path_of("storage.data_dir", "data") / "buffer_channel.json"
        save_channel_cache(cache, hit[0], hit[1])
        print(f"\n已写入缓存 {cache}：{hit[1].get('name')} / {channel_id}")
        return 0

    xs = [(o, c) for o, c in rows if (c.get("service") or "").lower() in ("twitter", "x")]
    cache = cfg.path_of("storage.data_dir", "data") / "buffer_channel.json"
    if len(xs) == 1:
        save_channel_cache(cache, xs[0][0], xs[0][1])
        print(f"\n自动选中唯一的 X 频道：{xs[0][1].get('name')} / {xs[0][1].get('id')}")
        print(f"已写入缓存 {cache}")
    elif len(xs) > 1:
        print(f"\n有 {len(xs)} 个 X 频道，请加 --channel-id 指定其中一个：")
        for _, c in xs:
            print(f"  python run.py buffer-setup --channel-id {c.get('id')}   # {c.get('name')}")
    else:
        print("\n没有连接 X 频道，请先在 Buffer 后台连上你的 X 账号")
    return 0



def cmd_probe(cfg: cfgmod.Config, limit: int, translate: bool) -> int:
    t = Translator(cfg)
    f = build_fetcher(cfg)
    posts = f.fetch(limit=limit)
    f.close()
    for p in posts:
        print("-" * 66)
        print(f"[{p.message_id}] {p.posted_at}  {p.url}")
        if p.media_urls:
            print(f"媒体: {len(p.media_urls)} 个 -> {p.media_urls[0][:90]}")
        print("原文:")
        print("  " + (p.text or "(空)").replace("\n", "\n  "))
        if translate and p.text.strip():
            print("英文:")
            print("  " + t.translate(p.text).replace("\n", "\n  "))
    print("-" * 66)
    print(f"共 {len(posts)} 条，最新 message_id = {posts[-1].message_id if posts else '-'}")
    return 0


def cmd_once(cfg: cfgmod.Config, dry_run: bool) -> int:
    p = Pipeline(cfg, dry_run=dry_run)
    try:
        r = p.cycle()
        print(f"\n结果: 抓到 {r['fetched']}，新帖 {r['new']}，发布 {r['published']}，"
              f"失败 {r['failed']}，跳过 {r['skipped']}")
        return 0 if r["failed"] == 0 else 2
    finally:
        p.close()


def cmd_preview(cfg: cfgmod.Config, channel: str) -> int:
    """释放指定频道最新一条（只删该频道），再跑一轮发出去。"""
    p = Pipeline(cfg)
    try:
        targets = ([channel.lstrip("@").lower()] if channel
                   else [c["name"] for c in p.channels])
        for ch in targets:
            row = p.store.conn.execute(
                "SELECT channel, message_id FROM messages "
                "WHERE lower(channel)=lower(?) ORDER BY message_id DESC LIMIT 1",
                (ch,),
            ).fetchone()
            if row:
                p.store.conn.execute(
                    "DELETE FROM messages WHERE channel=? AND message_id=?",
                    (row["channel"], row["message_id"]),
                )
                p.store.conn.commit()
                print(f"已释放最新一条: {row['channel']}:{row['message_id']}")
            else:
                print(f"频道 {ch} 没有可释放的记录（尚未建基线）")
        r = p.cycle()
        print(f"\n结果: 抓到 {r['fetched']}，新帖 {r['new']}，发布 {r['published']}，"
              f"失败 {r['failed']}，跳过 {r['skipped']}")
        return 0 if r["failed"] == 0 else 2
    finally:
        p.close()


def cmd_loop(cfg: cfgmod.Config, dry_run: bool, interval: int | None) -> int:
    p = Pipeline(cfg, dry_run=dry_run)
    try:
        p.loop(interval)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        p.close()
    return 0


def cmd_backfill(cfg: cfgmod.Config, limit: int, publish: bool) -> int:
    p = Pipeline(cfg)
    try:
        r = p.backfill(limit=limit, publish=publish)
        print(f"回填: {r}")
        return 0
    finally:
        p.close()


def cmd_stats(cfg: cfgmod.Config) -> int:
    from tg2x.store import Store
    s = Store(cfg.path_of("storage.data_dir", "data"))
    print(f"数据库: {s.db_path}")
    for k, v in sorted(s.stats().items()):
        print(f"  {k:<12} {v}")
    print(f"最新已见 message_id: {s.last_seen_id(str(cfg.get('channel', '')).lstrip('@').lower())}")
    return 0


def cmd_login(cfg: cfgmod.Config) -> int:
    """telethon 首次登录（会要手机验证码）。"""
    import os
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        print("未安装 telethon：pip install telethon")
        return 1
    api_id = os.environ.get("TG_API_ID", "")
    api_hash = os.environ.get("TG_API_HASH", "")
    phone = os.environ.get("TG_PHONE", "")
    if not (api_id and api_hash and phone):
        print("请先在 .env 里配置 TG_API_ID / TG_API_HASH / TG_PHONE")
        return 1
    session = str(cfg.path_of("storage.data_dir", "data") / "tg2x")
    with TelegramClient(session, int(api_id), api_hash) as client:
        client.start(phone=phone)
        me = client.get_me()
        print(f"登录成功：{me.first_name} (@{me.username})  会话已保存到 {session}.session")
    return 0


def cmd_payload(cfg: cfgmod.Config, limit: int) -> int:
    """把最新一条帖子走完整流程，只打印将要提交给下游的 JSON，不发。"""
    import json as _json

    from tg2x.models import TransPost
    from tg2x.publisher import Publisher

    t = Translator(cfg)
    f = build_fetcher(cfg)
    posts = f.fetch(limit=limit)
    f.close()
    pub = Publisher(cfg)
    target = (cfg.get("publish.target") or "dry_run").lower()

    for p in posts[-limit:]:
        item = TransPost(post=p, text_en=t.translate(p.text))
        item.final_text = pub.build_text(item)
        print("=" * 66)
        print(f"[{p.message_id}] {p.url}")
        print("--- 最终正文 ---")
        print(item.final_text)
        if target == "buffer":
            assets = []
            if (cfg.get("publish.buffer.media") or "telegram_url").lower() == "telegram_url":
                assets = [{"image": {"url": u}} for u in p.media_urls[:4] if u.startswith("http")]
            body = {
                "text": item.final_text,
                "channelId": (cfg.get("publish.buffer.channel_id") or "<首次自动解析>"),
                "schedulingType": "automatic",
                "mode": cfg.get("publish.buffer.mode", "shareNow"),
                "assets": assets,
            }
            print("--- Buffer createPost input ---")
        else:
            body = pub.payload(item)
            print("--- 下游 payload ---")
        print(_json.dumps(body, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram 频道 -> X 自动搬运")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="全量自检")
    sp = sub.add_parser("probe", help="抓最新几条看看效果")
    sp.add_argument("--limit", type=int, default=3)
    sp.add_argument("--no-translate", action="store_true")

    sp = sub.add_parser("once", help="跑一轮")
    sp.add_argument("--dry-run", action="store_true", default=None)

    sp = sub.add_parser("preview", help="释放指定频道最新一条并发送")
    sp.add_argument("--channel", default="", help="频道名（默认释放所有频道的最新一条）")

    sp = sub.add_parser("loop", help="常驻轮询")
    sp.add_argument("--dry-run", action="store_true", default=None)
    sp.add_argument("--interval", type=int, default=None)

    sp = sub.add_parser("backfill", help="回填历史")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--publish", action="store_true")

    sp = sub.add_parser("payload", help="看将要提交给下游的 JSON（不发送）")
    sp.add_argument("--limit", type=int, default=1)

    sub.add_parser("bot-id", help="查自己的 TG chat_id（先给 bot 发 /start）")
    sub.add_parser("notify-test", help="测试 bot 通知通道")

    sub.add_parser("stats", help="数据库统计")
    sub.add_parser("login", help="telethon 首次登录")
    sp = sub.add_parser("buffer-setup", help="列出 Buffer 组织/频道并选定 X 频道")
    sp.add_argument("--org-id", default="")
    sp.add_argument("--channel-id", default="")

    args = ap.parse_args()
    setup_log()
    cfg = cfgmod.load(args.config)

    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "probe":
        return cmd_probe(cfg, args.limit, not args.no_translate)
    if args.cmd == "once":
        return cmd_once(cfg, args.dry_run)
    if args.cmd == "preview":
        return cmd_preview(cfg, args.channel)
    if args.cmd == "loop":
        return cmd_loop(cfg, args.dry_run, args.interval)
    if args.cmd == "backfill":
        return cmd_backfill(cfg, args.limit, args.publish)
    if args.cmd == "stats":
        return cmd_stats(cfg)
    if args.cmd == "login":
        return cmd_login(cfg)
    if args.cmd == "bot-id":
        return cmd_bot_id(cfg)
    if args.cmd == "notify-test":
        return cmd_notify_test(cfg)
    if args.cmd == "payload":
        return cmd_payload(cfg, args.limit)
    if args.cmd == "buffer-setup":
        return cmd_buffer_setup(cfg, args.org_id, args.channel_id)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
