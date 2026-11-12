# tg2x — Telegram 频道 → 英文翻译 → Buffer → X

监控你关注的 Telegram 频道 **@YMISH3333**，一有新帖就自动：
原文归档 → 清洗推广尾巴 → 翻成英文 → 配图跟着走 → 灌进 Buffer → Buffer 发到 X。

> **要部署到 GitHub 上自动跑？看 [DEPLOY.md](DEPLOY.md)。**
> 顺带说明：`@SmartFinNewsBot` 这个 bot **读不了源频道**（你不是频道管理员，bot 进不去），
> 它在项目里只做「发布回执通知」，详见 DEPLOY.md 第 1 节。

---

## 一、关于「能不能连 TG」——能，但你只能用这两种

你不是这个频道的管理员，所以 **Bot API 那条路走不通**（Bot API 只能收它自己成为管理员之后的消息）。
剩下两条都实测可用：

| 方式 | 需要什么 | 实时性 | 现状 |
|---|---|---|---|
| **web**（默认启用） | 什么都不用。抓 `t.me/s/YMISH3333` 公开网页 | 30 秒轮询 | ✅ 已跑通，最新 message_id 23868 |
| **telethon** | `api_id`/`api_hash` + 手机收一次验证码 | 秒级 | 代码就绪，`python run.py login` 即可切 |

你的机器**直连 Telegram 是通的**（t.me / api.telegram.org 均 200/302），不用挂代理。

**什么时候要换 telethon？** 频道突然关闭网页预览、或转成私有、或者你要求秒级实时。改 `config.yaml` 的
`fetch.backend` 为 `telethon`，在 `.env` 填三个值，跑一次 `login`，其余流程完全不用动。

---

## 二、Buffer 是怎么接的（这是这次的关键改动）

Buffer 2026 版 API 是 **GraphQL 单端点**（`https://api.buffer.com`），Bearer 个人 key。
一个 `createPost` mutation 就能建帖，Buffer 负责投递到 X：

```graphql
mutation {
  createPost(input: {
    text: "..."
    channelId: "..."
    schedulingType: automatic
    mode: shareNow          # 立即发；addToQueue=排进队列空档
    assets: [{ image: { url: "https://..." } }]
  }) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
```

**踩到的坑：Buffer 不支持文件上传，图片必须是公网可访问 URL。**
好消息是不用自建图床——Telegram 的 CDN 链接（`cdn5.telesco.pe/...`）本身就是公开的，
直接作为 `assets.url` 传过去就行，Buffer 会自己去下载。这省掉了一整套对象存储。

配置里的开关：`publish.buffer.media: telegram_url`（默认）或 `none`（只发文字）。

### 三步接上你的 Buffer

```bash
# 1) Buffer 后台 → Settings → API → 生成 personal API key
# 2) 写进 .env
echo 'BUFFER_TOKEN=你的key' >> .env

# 3) 列出组织和频道，选定要发到哪个 X 频道（会自动缓存）
python run.py buffer-setup
```

`buffer-setup` 会打印所有组织下的频道和 id；如果只有一条 X 频道就自动选中，
有多条会提示你用 `--channel-id xxx` 指定。选定结果缓存在 `data/buffer_channel.json`。

---

## 三、跑起来

```bash
cd tg2x

python run.py doctor              # 自检：TG 连通 / 频道读取 / 翻译 / Buffer 通道
python run.py probe --limit 3     # 看效果：原文 + 英文译文，不发帖
python run.py payload --limit 1   # 看将要提交给 Buffer 的 JSON（重点看 text 和 assets）
python run.py once                # 跑一轮，真发（先跑 payload 确认内容没问题）
python run.py loop                # 常驻，30 秒一轮
```

依赖：`requests`、`beautifulsoup4`、`lxml`、`PyYAML`（本机已装齐）。
telethon 模式才需要 `pip install telethon`。

---

## 四、实际效果（真实帖子）

原文（@YMISH3333）：

```
#英国新闻
惊爆！美军军官英国被捕，涉嫌为俄情报机构搜集机密

英国苏格兰场拘捕一名 41 岁美军高级军官，涉嫌违反当地《2023 年国家安全法》…
📢  订阅全球大事件曝光
🔗 t.me/ymish3333
😘 投稿爆料：@DJNB009
```

发出去的英文（推广尾巴已剥掉，配图随帖）：

```
#UKNews
Shocking! U.S. Army officer arrested in the UK on suspicion of collecting
secrets for Russian intelligence agency

Scotland Yard arrested a 41-year-old senior US military officer on suspicion
of violating the National Security Act 2023…

https://t.me/ymish3333/23868
```

---

## 五、设计上的取舍（都是实测踩出来的）

1. **首轮不刷屏**：第一次运行只把频道现有 16 条记为基线，不发。只发接入之后的新帖。
   要连历史一起发：`python run.py backfill --limit 20 --publish`。
2. **推广尾巴必须剥**：这个频道每条都以「📢 订阅… / 🔗 t.me/… / 😘 投稿爆料：@DJNB009」结尾。
   用**从末尾逐行剥离**而不是全局正则——全局正则会误删正文里出现"投稿""频道"字样的正常句子
   （实测把一条单行短帖整条删掉了）。判定条件：emoji 开头 / 含 t.me 链接 / 独立 @账号 /
   以"关注·订阅·投稿…"开头的短句。
3. **翻译引擎要两个**：`translate.googleapis.com/translate_a/single` 这个经典免费端点在
   脚本调用下**很容易 429**（我用 curl 能通、用 Python 必 429，是客户端指纹问题）。
   现在主用 `clients5.google.com/translate_a/t`，失败再退回 gtx，各带 2 次退避重试，
   请求之间还有 `min_interval` 节流。还留了 `fallback: mymemory` 的应急档。
4. **原文一定留档**：每条帖子发之前先落 `data/archive/日期/频道_id.json`（原文+译文+最终正文），
   图片下到 `data/media/`。翻译错、发失败、复盘都有据可查。
5. **配图上限 4 张**（X 单条上限），多图帖只取前 4 张。
6. **失败不自动重试**：`status=failed` 的记录不会被再次发送，避免重复刷推。要补发就删掉那行。

---

## 六、目录结构

```
tg2x/
├── run.py                      命令行：doctor / probe / payload / once / loop / backfill / stats / buffer-setup / login
├── config.yaml                 全部配置
├── .env.example                密钥模板
├── tg2x/
│   ├── config.py               配置 + .env 加载
│   ├── models.py               TgPost / TransPost
│   ├── store.py                sqlite 去重 + 原文归档
│   ├── translator.py           中→英（clients5/gtx 双端点 + 重试节流 + MyMemory 兜底）
│   ├── buffer_client.py        Buffer GraphQL 客户端（组织/频道查询 + createPost）
│   ├── publisher.py            发布层（buffer / command / http / dry_run）
│   ├── pipeline.py             抓取 → 去重 → 归档 → 翻译 → 发布
│   └── fetchers/
│       ├── web.py              t.me/s/ 网页解析（零凭证）
│       ├── telethon_fetcher.py MTProto 用户会话
│       └── bot_fetcher.py      Bot API（你当前用不上，留给频道主场景）
├── examples/x_poster_example.py  备选：直连 X 发文的适配器骨架
├── tests/smoke_test.py           离线测试（不发真帖）
└── data/
    ├── tg2x.sqlite3            去重 + 状态
    ├── buffer_channel.json     选定的 Buffer X 频道
    ├── archive/2026-11-12/YMISH3333_23868.json
    ├── media/                  下载的配图
    └── tg2x.log
```

---

## 七、常见问题

**Q：`python run.py once` 说「新帖 0，跳过 16」？**
正常的——那是首轮基线。之后频道每发一条新帖就会处理一条。

**Q：Buffer 那边报 `MutationError: Channel not found`？**
`data/buffer_channel.json` 里的 id 失效了（频道被重新连接过会变 id）。删掉这个文件重跑 `buffer-setup`。

**Q：Buffer 发出去只有文字没有图？**
`assets.url` 里的 Telegram CDN 链接失效或被墙。Buffer 是海外服务器，取 `telesco.pe` 一般没问题；
如果频繁丢图，把 `publish.buffer.media` 设成 `none` 或者换成自己的对象存储 URL。

**Q：想连历史一起搬？**
`python run.py backfill --limit 30`（只归档不发），确认没问题再加 `--publish`。

**Q：翻译质量不够？**
`translate.provider` 改 `llm`，填 `OPENAI_API_KEY`。免费端点在"小妹店""磨丁"这类黑话和地名上会翻车。

---

## 八、两条注意

- **转载来源**：`include_source_link: true` 默认保留 `t.me` 原文链接，算是给出处。搬运他人内容注意版权。
- **内容合规**：X 对赌博、灰产、涉黄内容限制严格，被限流/封禁的风险落在**你的 X 账号**上。
  `config.yaml` 里 `cleanup.block_keywords` 可以填要拦掉的词（命中即整条跳过不发），建议按需打开。
