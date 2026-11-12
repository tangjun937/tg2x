# 部署到 GitHub Actions

目标：让 tg2x 在 GitHub 的服务器上每 10 分钟自己跑一次，不用开着电脑。

---

## 0. 先把泄露的 token 换掉（重要，别跳过）

你那个 bot token 已经贴在聊天里了，等于公开。任何人都能拿它给你的 bot 发消息、改 bot 名字。
**先做这一步：**

1. Telegram 里找 [@BotFather](https://t.me/BotFather)
2. 发 `/revoke` → 选 `@SmartFinNewsBot` → 选生成新 token
3. 把新 token 复制出来，后面填进 GitHub Secrets 和本地 `.env`

**新 token 只写两个地方**：GitHub 仓库的 Secrets、你本机的 `.env`。
永远不要写进 `config.yaml`、代码、或者 commit 上去。

---

## 1. 这个 bot 到底能干什么（先说清楚，免得白折腾）

你**不是** @YMISH3333 的管理员，bot 进不了那个频道，所以：

- ❌ bot **读不到**源频道的内容 —— 抓取仍然靠网页解析（`t.me/s/YMISH3333`），不需要 bot
- ✅ bot 能做**通知回执**：每次发完告诉你发了几条、失败了没有、错在哪

所以 bot 现在的角色是「监控面板」，不是「数据源」。这个已经接好了，跑完会自动给你发消息。

---

## 2. 仓库选 public 还是 private（这个直接影响能不能白跑）

免费额度的算法很实在，先算清楚：

| 方案 | 每月运行次数 | 每月消耗分钟 | 结论 |
|---|---|---|---|
| **public 仓库 + 10 分钟间隔** | 4320 | Actions 免费不限 | ✅ 推荐 |
| private 仓库 + 10 分钟间隔 | 4320 | ≈ 4300 分钟 | ❌ 免费额度 2000 分钟，撑不到月中 |
| private 仓库 + 30 分钟间隔 | 1440 | ≈ 1450 分钟 | ⚠️ 勉强够，但很紧 |

**建议用 public 仓库。** 代码里没有任何密钥（token 都在 Secrets 里，不随代码泄露），
公开不影响安全。真要 private，就把 workflow 里的 `cron` 改成 `*/30 * * * *`。

---

## 3. 创建仓库并推上去

你的 GitHub 是用谷歌账号（唐君）登录的，先确认两件事：用户名是什么、有没有开 2FA。
打开 <https://github.com> 右上角头像 → 能看到 `@你的用户名`。

然后在网页上建一个空仓库（**不要**勾选 Add README / .gitignore），名字随意，比如 `tg2x`。

回到本机，把项目推上去（把 `你的用户名` 换成实际的）：

```bash
cd D:/2026-11-12-16-57-36/tg2x

git init
git add .
git commit -m "tg2x: Telegram 频道 -> 英文翻译 -> Buffer -> X"
git branch -M main
git remote add origin https://github.com/你的用户名/tg2x.git
git push -u origin main
```

> push 时会要密码 —— 不是登录密码，是 **Personal Access Token**：
> GitHub 头像 → Settings → Developer settings → Personal access tokens → Tokens (classic)
> → Generate new token → 勾 `repo` → 生成后复制，粘贴到密码位置。

如果你更想用命令行登录，装个 GitHub CLI 后 `gh auth login` 一路回车也行。

---

## 4. 加三个 Secrets

仓库页面 → **Settings** → 左侧 **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value | 从哪来 |
|---|---|---|
| `BUFFER_TOKEN` | Buffer 个人 API key | Buffer 后台 → Settings → API |
| `TG_BOT_TOKEN` | 新的 bot token | 第 0 步 revoke 后拿到的 |
| `TG_CHAT_ID` | 你自己的数字 id | 见下面 |

`TG_CHAT_ID` 怎么拿（这步只能你自己做，别人代不了）：

```bash
# 1) 本机 .env 里先填 bot token
echo 'TG_BOT_TOKEN=新的token' > .env

# 2) Telegram 里给 @SmartFinNewsBot 发一句 /start

# 3) 跑这条，会打印你的 chat_id
python run.py bot-id
```

把打印出来的数字（比如 `123456789`）填成 `TG_CHAT_ID`。

顺手在本地验一下通知通道：

```bash
python run.py notify-test     # 手机会收到「通知通道已接通」
```

---

## 5. 先手动跑一次（dry run）

仓库页面 → **Actions** 标签 → 左边选 `tg2x` → 右边 **Run workflow** →
把 `dry_run` 勾成 **true** → 绿色按钮。

跑完点进这次运行，能看到：

- `跑一轮` 这步的输出：抓到几条、新帖几条
- 手机收到一条 Notification？dry run 不会发（因为没有真实发布），正常

**dry run 通过之后**，再跑一次 `dry_run` 不勾的运行，就是真发了。之后再靠 cron 自动跑。

---

## 6. 关于「自动」的两个真相

**① GitHub 的定时任务不准时。**
`*/10` 只是「最快 10 分钟」，高峰期经常延迟 5-20 分钟。也就是说：频道发帖 → 你 X 上出现，
实际延迟可能在 **10-30 分钟**之间。新闻类内容能接受，但别指望秒级。

**② 60 天没有提交，定时任务会被自动停掉。**
GitHub 会给仓库发邮件提醒。解决办法：随便改个文件提交一次，或者干脆每两个月手动 Run 一次。

如果这两点都不能接受，那就别用 Actions，换常驻方案：

| 方案 | 成本 | 实时性 |
|---|---|---|
| 自己的服务器 / 云主机跑 `python run.py loop` | 一台机器 | 30 秒 |
| Cloudflare Workers Cron（免费，1 分钟粒度） | 0 | 1-2 分钟 |
| Fly.io / Render 免费实例 | 0 | 需常驻，免费版会休眠 |

需要的话我再帮你搬。

---

## 7. 状态是怎么记住的（别踩这个坑）

去重状态在 `data/tg2x.sqlite3`，但 Actions 每次跑完容器就销毁了。
workflow 里用 `actions/cache` 把这个文件在运行之间传递：

- 跑之前 `restore`，用 `restore-keys: tg2x-state-` 捞最近一次
- 跑完 `save`，key 带 `run_id`（不然 key 重复会报错）

**万一 cache 丢了怎么办？** 不会重复刷屏 —— 代码里有 `boot_done:<频道>` 标记，
状态为空时第一次运行会只做「基线」（把现有帖子记为已见，不发），从下一条新帖才开始发。
这是刻意设计的兜底。

缓存也**只存这两个文件**（sqlite + buffer 频道 id），不存图片和归档，否则几天就撑爆 10GB 上限。

---

## 8. 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `Buffer 认证失败（401）` | `BUFFER_TOKEN` 没加或填错 | 检查 Secret 名字大小写，重新生成 key |
| `Buffer 拒绝发布：Channel not found` | 缓存的 channelId 失效（Buffer 重连过账号会变） | 删掉 `data/buffer_channel.json`，本地跑 `python run.py buffer-setup` 后重新提交 |
| `Google 翻译失败（两个端点均已重试）` | Actions 出口 IP 被限流 | 正常偶发，下轮会重试；频繁出现就换 `translate.provider: llm` |
| 任务成功但 X 上没内容 | 可能有新帖但被 `block_keywords` 拦了，或 Buffer 队列卡住 | 看 Actions 日志 + Buffer 后台的 Queue |
| 一直「新帖 0」 | 频道确实没更新，或网页预览被关 | 本地跑 `python run.py probe --limit 3` 验证 |
| 手机收不到通知 | `TG_CHAT_ID` 错 / 没给 bot 发过 /start | `python run.py notify-test` 本地排查 |

---

## 9. 日常维护

```bash
python run.py probe --limit 3     # 看抓到的原文和译文
python run.py payload --limit 1   # 看要提交给 Buffer 的 JSON（配图对不对）
python run.py stats               # 本地库统计
```

Actions 里想看历史：仓库 → Actions → 选某次运行 → 看 `跑一轮` 的日志。
