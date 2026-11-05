# 大肥鱼 🐟

基于 QQ 开放平台官方接口的异步 Python 机器人。使用 `aiohttp` 自行实现 Access Token、Gateway、WebSocket 心跳、Identify、Resume 与被动消息回复，不依赖第三方机器人框架。

支持：

- QQ 群聊 `GROUP_AT_MESSAGE_CREATE` + 全量模式 `GROUP_MESSAGE_CREATE`（无需 @）
- QQ 频道 `AT_MESSAGE_CREATE`
- QQ 私聊 `C2C_MESSAGE_CREATE`（无需 @）
- OpenAI 兼容 AI 对话（`/chat/completions`，回复与判断可分别指定模型）
- 群聊先由 AI 判断消息重要性，避免每条都回复；连续 15 条未回复时主动插一句
- `/ping`、`/help`、`/表情包`、`/meme`、`/image`、`/context`、`/reset`
- 本地表情包上传 + 公网图片 URL 发送 + 接收图片交给多模态模型
- GIF 表情包按时间均匀抽取最多 4 帧交给视觉模型，能识别动作变化
- 群图片自动收藏进表情包库，AI 识别内容后按话题主动发送
- 被动回复严格携带触发消息的 `msg_id`
- 5000 / 10000（1w）/ 50000（5w）三档上下文窗口
- 断线重连与 Session Resume
- Access Token 提前刷新（提前量按剩余有效期自适应，避免频繁刷新）
- 日志轮转与优雅退出
- 守护脚本自动重启 + cron `@reboot` 开机自启动

## 目录结构

```text
.
├── bot.py                          # 启动入口、信号处理、生命周期
├── config.py                       # .env 配置加载
├── handlers/
│   └── message_handler.py          # 指令路由、关键词、图片发送
├── utils/
│   ├── auth.py                     # Access Token 获取/缓存/提前刷新
│   ├── ai.py                       # OpenAI 兼容 Chat Completions 客户端
│   ├── api.py                      # QQ HTTP API、上传与被动回复
│   ├── gateway.py                  # WebSocket 网关、心跳、Identify/Resume
│   ├── context_window.py           # 上下文窗口与裁剪
│   ├── memes.py                    # 表情包库、AI 识别、话题匹配
│   ├── web_fetch.py                # 公网网页抓取、正文提取、内网拦截
│   ├── text.py                     # @ 前缀清洗、URL 校验
│   └── logging_setup.py            # 控制台 + 文件日志
├── assets/memes/                   # 本地表情包库（群图片也会自动收藏到这里）
├── scripts/
│   ├── generate_placeholder_memes.py
│   └── dafeiyu-service.sh          # start/stop/restart/status 守护脚本
├── tests/                          # 无需凭据即可运行的基础测试
├── data/                           # 上下文设置、表情包索引 memes.json
├── logs/                           # dafeiyu.log
├── requirements.txt
├── .env                            # 本地敏感凭据（已被 git 忽略）
└── .env.example
```

## 快速开始

### 1. 安装依赖

```bash
cd /opt/dafeyu/workspace
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

当前工作区已经创建 `.venv` 并安装好依赖。`Pillow` 已加入运行依赖，用于给 GIF 表情包均匀抽取动画帧。

### 2. 配置凭据

```bash
cp .env.example .env   # 如果还没有 .env
vim .env
```

至少填写：

```dotenv
APP_ID=你的AppID
APP_SECRET=你的AppSecret
```

`.env` 是唯一存放凭据的位置，代码中没有硬编码。`.env` 已加入 `.gitignore`，不要提交到仓库。

### 3. 生成占位表情包（可选）

```bash
python scripts/generate_placeholder_memes.py --force
```

会生成：

- `assets/memes/dafeiyu_01.png`
- `assets/memes/dafeiyu_02.png`

也可以把自己的 `png/jpg/jpeg/gif/webp/bmp` 图片直接放进 `assets/memes/`。

### 4. 环境自检

```bash
python bot.py --check
```

检查 Python 版本、依赖导入、`.env` 路径、凭据是否填写和表情包目录，不会连接 QQ。

### 5. 启动

```bash
source .venv/bin/activate
python bot.py
```

后台运行与守护：

```bash
scripts/dafeiyu-service.sh start     # 启动（崩溃后 10 秒自动重启）
scripts/dafeiyu-service.sh status    # 查看状态
scripts/dafeiyu-service.sh restart   # 重启
scripts/dafeiyu-service.sh stop      # 停止
```

进程号写入 `data/dafeiyu.pid`，守护日志写入 `logs/service.log`。机器人收到 `SIGTERM` / `SIGINT` 后，会关闭 WebSocket、取消心跳与消息任务后退出。

## 开机自启动

当前环境没有可用的用户级 systemd 会话，也没有 sudo 权限创建系统服务，因此使用 **cron** 拉起和看护：

```cron
# 开机启动
@reboot /opt/dafeyu/workspace/scripts/dafeiyu-service.sh start >>/opt/dafeyu/workspace/logs/service.log 2>&1

# 每分钟检查一次；进程被外部清理后自动拉起
* * * * * /opt/dafeyu/workspace/scripts/dafeiyu-service.sh ensure >/dev/null 2>&1
```

安装或查看：

```bash
crontab -l
crontab -e
```

`ensure` 只在未运行时启动，不会重复拉起。它还会校验 PID 文件里的进程确实是本守护脚本，避免 PID 被系统复用后误判为“正在运行”。

开机后 `start` 会启动守护进程；如果网络尚未就绪导致 Token 获取失败，守护进程会每 10 秒重试，不会直接放弃。

取消自启动和看护：

```bash
crontab -e
# 删除 @reboot 和 ensure 两行
```

注意：cron 服务必须处于运行状态，可用 `systemctl is-active cron` 确认。

### 为什么不用 OpenCode 的 shell 直接启动

当前终端运行在 `opencode.service` 的 cgroup 里。OpenCode 服务重启时，systemd 会清理整个 cgroup，把从终端里启动的大肥鱼一起杀掉。通过 cron 启动后，守护进程属于 `cron.service` cgroup，不会再被 OpenCode 重启误伤。

## 查看日志

实时查看：

```bash
tail -f logs/dafeiyu.log
```

查看最后 200 行：

```bash
tail -n 200 logs/dafeiyu.log
```

日志同时输出到终端，文件按 10MB 轮转，保留 5 个历史文件。启动成功后会看到：

```text
Access Token 已刷新
收到 Hello，心跳周期 45.0 秒
已发送 Identify（intents=1107296256）
鉴权成功：bot=...
大肥鱼已进入事件循环
```

## 指令

| 指令 | 行为 |
| --- | --- |
| `/ping` | 回复 `pong! 大肥鱼已在线` |
| `/help` | 输出指令菜单 |
| `/表情包` 或 `/meme` | 从表情包库随机发送一张图片；`/表情包 关键词` 可按描述/标签查找，也支持“发个表情包看看”等自然语言 |
| `/表情包 保存 <名字>` | 把当前/引用的图片（或最近 10 分钟内的图片）存入表情包库，打上名字标签并立即发送一次 |
| `/image <URL>` 或 `/img` | 发送公网图片 URL；直接发送带图片扩展名的 URL 也可以，普通网页链接会交给 AI 读取 |
| `/context` | 查看当前上下文窗口和使用量 |
| `/context 5000` | 切换为 5000 字窗口 |
| `/context 1w` | 切换为 10,000 字窗口 |
| `/context 5w` | 切换为 50,000 字窗口 |
| `/reset` 或 `/清空` | 清空当前会话的聊天上下文 |
| `@大肥鱼 闭嘴` | 当前群聊进入 10 分钟闭嘴模式，期间不回复任何消息 |
| `@大肥鱼 允许说话` | 立即解除闭嘴模式 |

关键词：

- 你好 / 哈喽 / hello / hi
- 在吗 / 在不在
- 摸鱼 / 摸会儿鱼

私聊直接发送指令即可，不需要 @。群聊已同时监听“仅 @”和“接收所有消息”两种事件；群内不 @ 也能收到消息，但普通消息会先经过 AI 重要性判断，只有值得回复时才说话。前提是：**QQ 开放平台已为机器人开启“接收所有消息”**，否则平台不会下发 `GROUP_MESSAGE_CREATE` 事件。频道仍需 @ 机器人或回复机器人消息。

## 上下文窗口

每次收到消息后，机器人会：

1. 将该消息写入当前会话的历史缓冲；
2. 按当前窗口上限裁剪，只保留最近的字符；
3. 生成一个不超过窗口大小的上下文文本块；
4. 把这个块传入 `MessageHandler`（当前命令路由不依赖 LLM，但接口已保留）。

窗口大小按“字符”计算，不是 token。允许值固定为：

- `5000` 字
- `1w` = `10000` 字
- `5w` = `50000` 字

默认值由 `.env` 的 `CONTEXT_WINDOW` 控制。群聊、频道或私聊中均可用 `/context` 单独调整，设置会写入 `data/context_settings.json`，重启后仍然有效；聊天历史本身只保存在内存中，重启会清空。

## AI 对话（OpenAI 兼容）

支持任何 OpenAI 兼容的 `/chat/completions` 服务，例如阿里云 MaaS、DeepSeek 官方接口等。回复和群聊判断可以使用不同模型。

`.env` 中配置：

```dotenv
AI_API_KEY=sk-...
AI_BASE_URL=https://ws-483pxxzqr92gcgsp.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
# 正常聊天、主动插话
AI_MODEL=deepseek-v4.1-flash-1017
# 群聊重要性判断
AI_JUDGE_MODEL=qwen3.7-flash
# 关闭推理过程（MaaS / DashScope 的 enable_thinking 参数）
AI_ENABLE_THINKING=false
AI_ENABLED=true
AI_TIMEOUT=60
AI_MAX_TOKENS=800
AI_TEMPERATURE=1.0
FIXED_REPLIES_ENABLED=false
# 群聊先让 AI 判断消息重要性，再决定是否回复
GROUP_REPLY_JUDGE=true
GROUP_JUDGE_CONTEXT_CHARS=2000
GROUP_REPLY_COOLDOWN=3
GROUP_IDLE_SPEAK_EVERY=15
GROUP_WATER_HOURS=3
GROUP_WATER_CHECK_SECONDS=60
# 表情包库
MEME_AUTO_COLLECT=true
MEME_ANALYZE=true
MEME_SEND_ENABLED=true
MEME_SEND_COOLDOWN=90
MEME_MAX_FILES=200
MEME_CATALOG_LIMIT=12
```

当前行为：

- `AI_ENABLED=true` 且 Key 有效时，私聊、频道 @ 的普通文本，以及通过群聊判断的文本都会交给 AI。
- 正常聊天和主动插话使用 `AI_MODEL`。
- `AI_ENABLE_THINKING=false` 会向接口下发 `enable_thinking=false`，关闭 Qwen / DeepSeek 推理模型的思考过程，响应更快、token 更省；留空则不下发该参数，兼容不支持此参数的接口。
- 群聊普通消息（未 @ 机器人）会先额外调用一次 `AI_JUDGE_MODEL` 做“是否值得回复”的判断，只有判断为值得时才用 `AI_MODEL` 生成回复。
- `FIXED_REPLIES_ENABLED=false` 时，“你好 / 在吗 / 摸鱼”等固定关键词回复关闭，改由 AI 生成回复。
- `/ping`、`/help`、`/表情包`、`/meme`、`/image`、`/context`、`/reset` 仍是本地确定性指令，方便测试和控制。
- 每次请求会把当前会话裁剪后的上下文（5000 / 1w / 5w 字）作为最近聊天记录发给 AI，并将 AI 回复追加到上下文。
- AI 调用失败时会自动重试 3 次；仍失败时私聊/频道回复“AI 暂时连接失败了”，群里只对 @ 或引用机器人的消息回一句提示，普通闲聊保持沉默。
- 系统提示词可通过 `AI_SYSTEM_PROMPT` 覆盖；默认人设写在 `config.py` 的 `DEFAULT_AI_SYSTEM_PROMPT`。

### 默认人设

大肥鱼是“以 AI 为内核、长着蓝色鲸鱼尾巴的小萝莉”，软萌、机灵、有点嘴馋，最爱吃大白饭（因为能换成 tokens），主人是 `tony1026`。

人设里同时要求：

- 不复述、不总结对方刚说过的话，看懂就直接接话；
- 不用“你说的是……对吧”“所以你的意思是……”这类复读式开头；
- 卖萌看场合：聊正事、有人心情不好、在吵架时收着点；
- 不确定就直说，不编造群友信息。

默认 `AI_TEMPERATURE=0.8`，比 `1.0` 更稳，减少胡言乱语。

每次普通对话（私聊、频道、群聊回复）都会把这份人设作为 `system` 消息发给大模型；群聊重要性判断仍使用独立的判断提示词，不加载人设，避免浪费 token 和干扰判断。

### 补充背景（本机私有）

群专属的背景信息放在本地文件 `data/group_context.txt`，启动时会追加到系统提示词末尾：

```text
【补充背景】
这个 QQ 群是一个乐队群，乐队里的所有人（包括群友）都来自某中学。
```

- 每行一条，空行和 `#` 开头的注释会被忽略，最多读取 2000 字；
- 文件不存在就跳过，不影响启动；
- `data/` 已被 `.gitignore` 排除，所以学校、成员等只在本机生效，不会出现在公开仓库里；
- 改完文件后重启服务即可生效。

### 称呼真名

机器人默认优先称呼群友的真名，而不是网名或单字简称。昵称与真名的对应关系放在本地文件 `data/member_aliases.json`，该文件已被 `.gitignore` 排除，不会提交到公开仓库。格式：

```json
{
  "aliases": {
    "某个网名": "对应的真名"
  }
}
```

没有配置对应关系、或对方明确要求用网名时，机器人会继续使用聊天记录里的昵称，不会自行猜测。

动作和神态用中文全角括号表示，例如“（歪头）”；一条回复最多一个，不许每条都演小剧场。

私聊或群聊中的普通聊天内容会发送到 AI 服务，请勿在对话中发送密码、密钥等敏感信息。恢复纯规则模式可设置：

```dotenv
AI_ENABLED=false
FIXED_REPLIES_ENABLED=true
```

## 群聊回复判断

群聊开启“接收所有消息”后，机器人会收到大量与自己无关的闲聊。为避免刷屏，群聊普通消息会先经过一次 AI 判断：

- 判断输入：当前会话最近的聊天记录（默认最多 `GROUP_JUDGE_CONTEXT_CHARS=2000` 字）+ 最后一条消息 + 发送者。
- 判断输出：`reply`、`score`、`reason`，日志会记录 `群聊重要性判断 reply=... score=... reason=...`。
- 值得回复：明确 @ / 点名机器人、向机器人求助、延续与机器人的话题、明显的玩笑或求互动。
- 不值得回复：群友闲聊、对别人说的话、单表情/刷屏、广告链接、拿不准的内容。
- 拿不准时不回复，优先避免抢话。
- 判断请求本身失败（网络、超时、解析失败）时，本条消息保持沉默，避免 API 抖动导致连续回复。
- 所有 AI 请求遇到网络错误、429 或 5xx 时会自动重试 3 次（间隔 1、2 秒）；仍然失败时，`@机器人` 和引用机器人的消息会回一句“刚卡了一下”，不会让用户以为机器人没收到。
- 同一群内两次 AI 主动回复至少间隔 `GROUP_REPLY_COOLDOWN` 秒（默认 3 秒），防止消息爆发时连续刷屏；设为 `0` 可关闭冷却。@机器人和指令不受冷却限制。
- 群聊连续 `GROUP_IDLE_SPEAK_EVERY` 条消息没有得到回复时（默认 15 条），下一条会触发一次 AI 主动插话，避免长时间完全潜水；插话成功后计数清零，设为 `0` 可关闭。@机器人、指令、图片等任何成功回复都会清零计数。
- 机器人在某个群连续 `GROUP_WATER_HOURS` 小时没有发过言（默认 3 小时）时，会主动发一句短消息水群；每个群独立计时，闭嘴中的群会跳过，发送失败也不会立刻重试。检查间隔由 `GROUP_WATER_CHECK_SECONDS` 控制（默认 60 秒），把 `GROUP_WATER_HOURS` 设为 `0` 可关闭。
- 群内 `@大肥鱼 闭嘴` 会让该群进入 10 分钟闭嘴模式：期间所有消息（包括 @ 和指令）都不回复，也不触发主动插话；`@大肥鱼 允许说话`（或“可以说话 / 解除闭嘴 / 别闭嘴了”）会立即恢复。闭嘴状态只影响当前群，且不写入磁盘，重启服务后会解除。

以下情况绕过判断，直接处理：

- 所有 `/` 开头的指令；
- 群内明确 `@机器人` 的消息；
- 引用机器人自己消息的回复（即使没有 @；QQ 有时不上报被引用者，此时会按被引用的文字匹配机器人最近说过的话）；
- 私聊和频道消息（本身就是一对一或明确 @）；
- 裸图片 URL。

相关配置：

```dotenv
GROUP_REPLY_JUDGE=true
GROUP_JUDGE_CONTEXT_CHARS=2000
GROUP_REPLY_COOLDOWN=3
GROUP_IDLE_SPEAK_EVERY=15
GROUP_WATER_HOURS=3
GROUP_WATER_CHECK_SECONDS=60
```

关闭判断后，群聊会恢复为“每条普通消息都让 AI 回复”：

```dotenv
GROUP_REPLY_JUDGE=false
```

注意：群聊每条普通消息会消耗两次 AI 调用（判断模型一次、回复模型一次），判断上下文越长费用越高。若只想低成本运行，可把 `GROUP_JUDGE_CONTEXT_CHARS` 调小到 `800` 左右。

## 图片发送

### 本地图片

- 群聊 / 私聊：读取图片后优先走官方推荐的分片上传（`upload_prepare` → 预签名 URL PUT → `upload_part_finish` → 合并），获得 `file_info`，再以 `msg_type=7` + 原始 `msg_id` 发送；分片接口不可用时自动回退到 `multipart/form-data` 直传。
- 频道：使用频道消息接口的 `multipart/form-data` 与 `file_image` 字段发送。
- 文件缺失、格式不支持、空文件或超过 `MAX_LOCAL_IMAGE_MB` 时，会返回友好文字提示。
- 群聊 / 私聊本地上传默认上限为 5MB（官方直传建议值），可通过 `MAX_LOCAL_IMAGE_MB` 调整；分片上传本身支持更大的官方限制。

### 网络图片

- 频道：直接以 `image` URL 字段发送。
- 群聊 / 私聊：先调用对应场景的文件 URL 上传接口转存，再用 `file_info` 发送。
- 使用 `/image <URL>`、`/img <URL>`，或单独发送一个图片 URL。
- 仅允许 `http://` / `https://` 绝对地址。

### 接收图片（看图）

- 群聊、私聊、频道的图片消息会从事件 `attachments` 中解析出图片 URL，最多取前 4 张，作为多模态 `image_url` 输入传给 `AI_MODEL`。
- 图片会按会话缓存 10 分钟；后续消息提到“图片 / 这张图 / 刚才的图 / 截图 / 画像”等时，会自动带上最近的图片。
- 远程图片会先由机器人下载并转成 base64 内联数据再发给模型，避免模型服务无法访问 QQ 的签名 CDN 链接。
- 只有图片没有文字时，上下文里记为 `[图片]`；直接 @ 机器人发图会触发看图回复。
- 聊天模型必须支持 `image_url` 内容。当前 `deepseek-v4.1-flash-1017` 已实测支持图片输入（返回 `image_tokens`）。
- 图片本身不会经过 WebSocket 传输，WebSocket 事件里只有图片 URL。

群友发的“表情包”通常也是图片附件（`image/gif`、`image/jpeg` 等），机器人回复时会下载并交给视觉模型，所以能看到画面；GIF 会按时间顺序均匀抽取最多 4 帧一起交给模型，因此能看出动作变化，而不是只看首帧。但要注意：

- 没有 @ 机器人的纯表情包消息，重要性判断器只能看到文本 `[图片]`，可能选择不回复；这不代表看不到，而是没有触发回复。
- 直接 @ 机器人发表情包，或引用该表情包问“这个表情什么意思”，会带上图片给模型。
- 后续追问里出现“这个表情包 / 刚才的表情 / 贴图 / 梗图”等关键词时，会自动带上最近 10 分钟内的表情包图片。
- QQ 自带小黄脸（`<faceType=...>`）不是图片附件，只是文本标签，机器人看不到具体画面。
- “这个表情包是什么意思”会交给 AI 解释；只有“发 / 来 / 给 / 要 / 斗图”等请求才会触发随机发表情包。

### 网页读取（webfetch）

当群友 **@机器人并带链接**、在私聊里发链接，或引用机器人的消息带链接时，机器人会抓取网页文字，附加到这一次回复的提示词里：

- 只抓 1 个链接，只取标题、摘要和正文前 `WEB_FETCH_MAX_CHARS`（默认 800）字；
- 网页内容只用于当前回复，**不会写入聊天历史**，后续消息不会重复计费；
- 正文太少、非 HTML、超时、状态码异常或非公网地址时直接跳过；
- 拒绝 `127.0.0.1`、内网 IP、链路本地地址等，避免 SSRF；
- 最多跟随 3 次重定向，下载上限默认 512 KB，超时默认 8 秒；
- 同一链接缓存 10 分钟，避免重复抓取；
- 不 @ 机器人的普通群消息即使带链接也不会读取，避免每个链接都消耗 token；
- 微信公众号、知乎、B 站等依赖 JS 的页面，可能只拿到标题和摘要。

相关配置：

```dotenv
WEB_FETCH_ENABLED=true    # 关闭后完全不抓网页
WEB_FETCH_MAX_CHARS=800   # 每次回复最多附加的正文字数，0=关闭
WEB_FETCH_TIMEOUT=8       # 单次抓取超时（秒）
WEB_FETCH_MAX_BYTES=524288
```

按当前实测比例，800 字中文正文大约增加 600 输入 tokens，只在实际回复带链接的消息时产生。

### 引用回复（看懂“这句话”）

QQ 群聊的引用回复会在事件里携带 `message_scene.ext`（`msg_idx` / `ref_msg_idx`）和 `msg_elements`。机器人会：

- 从 `msg_elements` 中取出被引用的原消息（`message_type=103`，也兼容事件级 `message_type=103` 的客户端）；
- 同时按 `ref_msg_idx` 在本地最近 80 条消息缓存里兜底查找；
- 把引用内容直接标注在当前消息下面：

```text
[14:26] Tony1026: 他这句话说的是什么？
（Tony1026 引用的是 仿生人 的这条消息：「今天天气不错」）
```

这样模型解释“这句话 / 他说的什么意思”时会以引用内容为准，不会误用聊天记录里的上一条。引用图片也会作为多模态图片一起传给模型。如果客户端没有上报引用内容，机器人会直接说看不到，而不是猜一句。

日志里可以用 `引用结构` 关键字确认 QQ 实际下发的字段：

```text
引用结构 | ext={'msg_idx': '...', 'ref_msg_idx': '...'} | message_type=103 | elements=['103'] | parsed=仿生人/今天天气不错
```

### 表情包库（自动收藏 + AI 识别 + 主动发送）

表情包目录：

```text
/opt/dafeyu/workspace/assets/memes/
```

直接把 `png` / `jpg` / `jpeg` / `gif` / `webp` / `bmp` 图片放进去即可。机器人启动时会扫描目录，并为每张还没有描述的图片调用一次视觉模型，判断它是否适合当表情包，再生成简短画面描述、3-6 个中文标签、主要情绪、适用场景和风险等级，索引保存在 `data/memes.json`。群聊中群友发送的图片也会自动下载、去重并加入这个目录：

- 收藏群聊图片，支持群聊接口可发送的 `png` / `jpg` / `jpeg` / `gif`（GIF 已实测可上传发送，会保留动图）；引用消息里的图片也会被收藏；
- 下载后先让视觉模型判断是否适合当表情包：普通食物照、饮料照、风景照、普通自拍、商品图、聊天截图、二维码、文档等会被直接丢弃，不写入索引；
- 手动用 `/表情包 保存 <名字>` 收藏的图片会保留，哪怕 AI 认为它不是典型表情包；
- GIF 表情包会按时间顺序均匀抽取最多 4 帧（包含首尾帧，去重后最多 4 帧），再交给视觉模型识别动作、表情和情绪；单帧 GIF 或非 GIF 图片仍按单张识别；
- 每张图会生成 `emotion`（主要情绪，如无语/震惊/围观/期待）、`use_when`（2-4 个具体使用场景）和 `risk`（low/medium/high）；候选列表会把这些信息一起交给聊天模型；
- 按 SHA-256 去重，超过 `MEME_MAX_FILES`（默认 200 张）后不再收藏；
- 启动或刷新时会校验目录内所有图片的 SHA-256：内容相同的文件只保留一份，优先保留已经有描述/标签的那份，其余副本会从索引和磁盘删除；
- 识别为普通照片、聊天截图、二维码、文档、表格、证件等内容的图片会被自动移除，避免把无关图片或隐私截图当表情包；
- 抓取和识别在后台进行，不阻塞正常回复；
- 不想自动收藏时设置 `MEME_AUTO_COLLECT=false`。

每次 AI 回复前，机器人会按描述、标签、情绪和使用场景匹配当前话题，最多把 `MEME_CATALOG_LIMIT`（默认 12）张候选交给模型。只要情绪或场景对得上，模型就会更积极地配一张；普通闲聊、吐槽、接梗、庆祝、围观都会考虑，但严肃提问、争论、安慰和隐私话题不会发。模型决定使用时会回复一个内部标记，机器人再把文字和图片一起发出：

```text
[MEME:group_1a2b3c4d5e6f.png]
```

相关配置：

```dotenv
MEME_AUTO_COLLECT=true     # 自动收藏群图片
MEME_ANALYZE=true          # 用视觉模型识别每张表情包
MEME_SEND_ENABLED=true     # 允许 AI 在聊天中主动发表情包
MEME_SEND_COOLDOWN=30      # 同一会话两次主动发表情包的最小间隔（秒）
MEME_MAX_FILES=200         # 表情包库上限
MEME_CATALOG_LIMIT=12      # 每次给模型看的候选数量
```

注意：

- 表情包识别会产生额外的视觉模型调用和图片 token；图片越多，启动时的识别耗时越长。
- 自动收藏会把群里的图片保存到服务器本地，请确认群友知情；不需要时关闭 `MEME_AUTO_COLLECT`。
- 主动发送有 90 秒冷却，且每次回复最多一张，避免斗图刷屏。
- 模型没有选表情包时只发文字，这是正常行为；想立刻要一张可以用 `/表情包`，按名字找可以用 `/表情包 哈哈哈`。
- 群里的图片既可以直接发，也可以引用后让机器人收藏；引用图片会被重新下载并按 SHA-256 去重。

所有发送都会携带触发消息的 `msg_id`，满足平台被动消息风控。群聊/私聊同一消息的多次回复会自动递增 `msg_seq`。私聊被动回复有效期为 60 分钟、每条消息最多 4 次；群聊为 5 分钟、最多 5 次。

## 鉴权与网关实现

### Access Token

- 主接口：`https://bots.qq.com/app/getAppAccessToken`
- 主接口不可用时自动回退：`https://api.bot.qq.com/app/getAppAccessToken`
- 凭据以 JSON 提交：`{"appId": "...", "clientSecret": "..."}`
- 缓存 `expires_in`，提前 `TOKEN_REFRESH_MARGIN`（默认 300 秒）刷新
- 若平台返回的剩余有效期短于提前量，按有效期的 1/3 动态缩短提前量，避免每秒重复刷新
- 后台刷新协程保证空闲时也不会带着过期 Token 重连
- 每次 HTTP 请求仍会调用 `get_token()`，即使后台刷新失败也能继续使用尚未过期的旧 Token
- HTTP 401 时自动强制刷新并重试一次

### WebSocket

请求 `/gateway` 动态获取地址，然后：

1. 接收 `OpCode 10 Hello`，读取 `heartbeat_interval`；
2. 启动心跳协程，定时发送 `OpCode 1`；
3. 发送 `OpCode 2 Identify`，Token 格式为 `QQBot <access_token>`；
4. 收到 `READY` 后保存 `session_id`；
5. 断线后优先发送 `OpCode 6 Resume`，用最新 `seq` 补发事件；
6. `OpCode 9`、`4006/4007` 等无效 Session 场景自动清除 Session 并重新 Identify；
7. `4008/4009` 保留 Session 尝试 Resume；
8. `4914/4915`、`4013/4014` 等不可恢复错误会记录明确原因并停止；
9. 心跳未收到 ACK 时主动断开，避免半死连接。

事件处理使用独立协程，不会阻塞心跳；消息按 ID 去重，避免 Resume 补发导致重复回复。

## 开放平台配置

1. 在 QQ 开放平台创建机器人，取得 `AppID` / `AppSecret`。
2. 在“开发设置”中配置沙箱群/频道或正式发布。
3. 确保机器人拥有群聊消息、频道消息相关权限。
4. **群聊不 @ 也能回复**：在机器人配置中开启“接收所有消息”（全量消息模式）。未开启时平台只推送 `GROUP_AT_MESSAGE_CREATE`，代码无法凭空收到普通群消息。
5. **私聊测试**：确认机器人已允许私聊/已添加好友，平台会推送 `C2C_MESSAGE_CREATE`；无需额外 Intents，复用 `GROUP_AND_C2C_EVENT (1<<25)`。
6. `.env` 默认 Intents 为：

   ```text
   (1 << 25) GROUP_AND_C2C_EVENT
   (1 << 30) PUBLIC_GUILD_MESSAGES
   = 1107296256
   ```

   如果只使用群聊或只使用频道，可以修改 `INTENTS`。如果开放平台没有对应权限，Identify 可能返回 `4013` / `4014`，需要先在平台申请权限。

## 测试

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
python -m compileall -q .
```

测试覆盖 @ 前缀清洗、URL 校验、上下文窗口裁剪/持久化、指令路由、AI 请求结构与回复/判断模型分离、群聊重要性判断、潜水计数器、AI 替换固定回复、群聊全量事件、私聊 C2C 事件、网页正文提取与内网地址拦截，以及群聊/频道/私聊被动回复中的 `msg_id` 与 `msg_seq` 载荷，不需要真实 QQ 凭据。

## 常见问题

- **启动提示缺少 APP_ID / APP_SECRET**：配置 `.env`，不要加引号以外的空格。
- **获取 Token 返回 100016**：AppID 或 AppSecret 不正确。
- **WebSocket 4013/4014**：Intents 无权限或值不合法，检查开放平台权限与 `INTENTS`。
- **群消息发不出**：确认机器人仍在群内、未被禁言，且回复距触发消息不超过 5 分钟。
- **群聊不 @ 没反应**：开放平台未开启“接收所有消息”时只能收到 @ 消息；开启后普通群消息才会由平台推送给机器人。
- **私聊发不出**：确认已添加机器人为好友且未关闭接收消息；错误码 `40054004` 表示无好友关系，`40054013` 表示用户拒收。
- **图片发送失败**：检查文件是否存在、是否超过 5MB、格式是否为平台支持的类型；网络图片需公网可访问。
- **频道被动回复失败**：频道被动回复有效期同样为 5 分钟，请确认事件处理没有长时间阻塞。
- **AI 401/鉴权失败**：检查 `AI_API_KEY` 是否有效、是否有多余空格。
- **AI 超时**：AI 接口较慢时可增大 `AI_TIMEOUT`；群聊被动回复必须在 5 分钟内完成。
- **群聊该回却不回**：查看日志里的 `群聊重要性判断`，若 `reply=false` 是模型判断为闲聊；可直接 @ 机器人绕过判断，或调大判断上下文、把 `GROUP_REPLY_JUDGE` 设为 `false`。若长时间不回复，检查 `GROUP_IDLE_SPEAK_EVERY` 是否为 `0`。
- **机器人一直不说话**：检查 `GROUP_WATER_HOURS` 是否为 `0`，以及日志里是否有“水群计时器已启动”。计时从机器人第一次在群里收到消息开始，闭嘴中的群不会水群；发送失败会等下一个周期再试。
- **群聊太吵**：确认 `GROUP_REPLY_JUDGE=true`，并可调大 `GROUP_REPLY_COOLDOWN`（例如 `15`）；若仍频繁回复，可在 `utils/ai.py` 的 `JUDGE_SYSTEM_PROMPT` 中进一步收紧“值得回复”的条件。
- **群图片没有进表情包库**：检查图片是否超过 `MAX_LOCAL_IMAGE_MB`、格式是否为群聊可发送的 png/jpg，以及 `MEME_AUTO_COLLECT` 是否为 `true`；日志会记录“新表情包已入库”或“识别为截图/二维码等，跳过”。
- **表情包不主动发**：确认 `MEME_SEND_ENABLED=true`、`data/memes.json` 里已有描述，且当前话题与标签匹配；也可以直接用 `/表情包` 手动发一张。
- **Token 接口差异**：官方文档目前同时存在 `bots.qq.com` 与 `api.bot.qq.com` 两个域名，本项目按需求优先使用前者，失败后自动回退。

## 许可证

本项目使用 [GNU General Public License v3.0](LICENSE)（GPL-3.0）许可协议。

Copyright (C) 2026 tony20071026
