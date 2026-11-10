# 大肥鱼 🐟

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

一只住在 QQ 群里的 AI 小萝莉。

基于 QQ 开放平台官方接口的异步 Python 机器人，使用 `aiohttp` 自己实现 Access Token、Gateway、WebSocket 心跳、Identify、Resume 和被动消息回复，不依赖第三方机器人框架。聊天模型走任何 OpenAI 兼容的 `/chat/completions` 接口。

它不只是一个“@ 一下就回一句”的机器人：会看群里的普通消息，自己判断该不该接话；会看图、读网页、引用回复；会把群友发的图收进表情包库，在合适的时候主动发一张。

## 目录

- [功能一览](#功能一览)
- [运行架构](#运行架构)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置参考](#配置参考)
- [指令](#指令)
- [聊天与上下文](#聊天与上下文)
- [群聊回复策略](#群聊回复策略)
- [图片与表情包](#图片与表情包)
- [网页读取](#网页读取)
- [引用回复](#引用回复)
- [鉴权与网关](#鉴权与网关)
- [部署与守护](#部署与守护)
- [QQ 开放平台配置](#qq-开放平台配置)
- [测试](#测试)
- [安全与隐私](#安全与隐私)
- [常见问题](#常见问题)
- [许可证](#许可证)

## 功能一览

| 分类 | 能力 |
| --- | --- |
| 消息接入 | 群聊 @、群聊全量消息、频道 @、私聊 C2C；WebSocket 断线重连与 Session Resume |
| AI 聊天 | OpenAI 兼容接口；回复与群聊判断可使用不同模型；失败自动重试 |
| 群聊策略 | 先判断消息是否值得回复；回复冷却；连续潜水后主动插话；长时间没说话主动水群；群内闭嘴模式 |
| 上下文 | 5000 / 1w / 5w 三档字符窗口；按会话独立设置并持久化 |
| 图片 | 接收图片交给多模态模型；本地图片分片上传；网络图片转存；GIF 抽帧识别 |
| 表情包 | 自动收藏群图、AI 审核与打标、按情绪/场景匹配、主动发送；SHA-256 去重 |
| 网页 | 只在 @、私聊或引用机器人时抓取；正文限长、内网地址拦截、结果不写入聊天历史 |
| 引用回复 | 解析 QQ 引用结构，区分“谁引用了谁的话” |
| 运维 | 控制台 + 文件日志、日志轮转、守护脚本、cron 开机自启与每分钟看护 |
| 测试 | `unittest` 覆盖消息路由、AI 请求、上下文、图片、表情包、网页抓取与被动回复载荷，不需要真实 QQ 凭据 |

## 运行架构

```text
                 QQ 开放平台
                      │
          WebSocket 事件 │ HTTP API
                      ▼
              utils/gateway.py
                      │
                      ▼
          handlers/message_handler.py
             │        │         │
             │        │         └──→ utils/web_fetch.py
             │        │
             │        ├──→ utils/context_window.py
             │        └──→ utils/ai.py ──→ OpenAI 兼容接口
             │
             ▼
          utils/api.py ──→ 群聊 / 频道 / 私聊
```

消息处理的顺序大致是：

1. 网关收到事件，去重、清洗 @ 前缀、解析图片与引用；
2. 指令直接走本地路由；
3. 群聊普通消息先交给判断模型，决定是否值得回复；
4. 值得回复时，把最近上下文、表情包候选、网页摘录和图片一起交给聊天模型；
5. 解析回复里的表情包标记，文字和图片分别通过被动回复接口发出。

## 项目结构

```text
.
├── bot.py                          # 启动入口、信号处理、生命周期
├── config.py                       # .env 加载与默认配置
├── handlers/
│   └── message_handler.py          # 指令路由、群聊判断、图片、表情包、网页
├── utils/
│   ├── auth.py                     # Access Token 获取、缓存与提前刷新
│   ├── ai.py                       # OpenAI 兼容客户端、人设、昵称与重试
│   ├── api.py                      # QQ HTTP API、上传、被动回复
│   ├── gateway.py                  # WebSocket、心跳、Identify / Resume
│   ├── context_window.py           # 上下文窗口、裁剪与持久化
│   ├── memes.py                    # 表情包库、AI 识别、话题匹配
│   ├── web_fetch.py                # 网页抓取、正文提取、内网拦截
│   ├── text.py                     # @ 前缀清洗、URL 校验
│   └── logging_setup.py            # 控制台 + 文件日志
├── assets/memes/                   # 自带表情包（群图收藏也在这里，但被 git 忽略）
├── scripts/
│   ├── generate_placeholder_memes.py
│   └── dafeiyu-service.sh          # start / ensure / stop / restart / status
├── tests/                          # 无需 QQ 凭据的单元测试
├── data/                           # 运行状态、索引、私有配置（整目录被 git 忽略）
├── logs/                           # 运行日志（被 git 忽略）
├── requirements.txt
├── .env                            # 本地凭据（被 git 忽略）
└── .env.example
```

`data/` 里常见的文件：

| 文件 | 用途 |
| --- | --- |
| `data/memes.json` | 表情包索引与 AI 标签 |
| `data/context_settings.json` | 各会话的上下文窗口设置 |
| `data/member_aliases.json` | 本地昵称 → 真名 / 固定叫法（私有） |
| `data/group_context.txt` | 群专属补充背景（私有） |
| `data/dafeiyu.pid` | 守护进程 PID |
| `data/dafeiyu.stop` | 停止标记 |

## 快速开始

### 环境要求

- Python 3.10+
- 能访问 QQ 开放平台和你的 AI 接口
- Linux（使用 `scripts/dafeiyu-service.sh` + cron 时）

### 1. 获取代码并安装依赖

```bash
git clone https://github.com/tony20071026/dafeiyu.git
cd dafeiyu

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`Pillow` 用于给 GIF 表情包抽帧；不装也能运行，但动图识别会退化为只看首帧。

### 2. 配置凭据

```bash
cp .env.example .env
vim .env
```

至少填写 QQ 开放平台的凭据：

```dotenv
APP_ID=你的AppID
APP_SECRET=你的AppSecret
```

再按需要填写 AI 接口：

```dotenv
AI_API_KEY=sk-...
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-chat
AI_ENABLED=true
```

`.env` 是唯一存放凭据的地方，代码中没有硬编码。它已被 `.gitignore` 排除，**不要提交到仓库，也不要把真实 Key 贴到 issue 里**。

### 3. 生成占位表情包（可选）

```bash
python scripts/generate_placeholder_memes.py --force
```

会生成两张占位图。也可以直接把自己的 `png` / `jpg` / `jpeg` / `gif` / `webp` / `bmp` 放进 `assets/memes/`。

### 4. 环境自检

```bash
python bot.py --check
```

检查 Python 版本、依赖、`.env` 路径、凭据是否填写和表情包目录，不会连接 QQ。

### 5. 启动

前台运行，方便看日志：

```bash
source .venv/bin/activate
python bot.py
```

后台守护：

```bash
scripts/dafeiyu-service.sh start     # 启动（崩溃后 10 秒自动重启）
scripts/dafeiyu-service.sh status    # 查看状态
scripts/dafeiyu-service.sh restart   # 重启
scripts/dafeiyu-service.sh stop      # 停止
```

进程号写入 `data/dafeiyu.pid`，守护日志写入 `logs/service.log`。机器人收到 `SIGTERM` / `SIGINT` 后会关闭 WebSocket、取消心跳与消息任务再退出。

## 配置参考

完整模板见 [`.env.example`](.env.example)。下面是最常用的配置。

### QQ 与网关

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ID` | 空 | QQ 开放平台机器人 AppID，必填 |
| `APP_SECRET` | 空 | QQ 开放平台机器人 AppSecret，必填 |
| `BOT_NAME` | `大肥鱼` | 机器人昵称，用于清洗 @ 前缀 |
| `INTENTS` | `1107296256` | `(1<<25) GROUP_AND_C2C_EVENT | (1<<30) PUBLIC_GUILD_MESSAGES` |
| `QQ_API_BASE` | `https://api.sgroup.qq.com` | 官方 API 根地址 |
| `TOKEN_URL` | `https://bots.qq.com/app/getAppAccessToken` | 主 Token 接口 |
| `TOKEN_FALLBACK_URL` | `https://api.bot.qq.com/app/getAppAccessToken` | 主接口失败时的回退 |
| `TOKEN_REFRESH_MARGIN` | `300` | 提前刷新 Token 的秒数 |
| `HTTP_TIMEOUT` | `30` | HTTP 请求超时 |
| `WS_HELLO_TIMEOUT` | `20` | 等待网关 Hello 的超时 |
| `CONTEXT_WINDOW` | `5000` | 默认上下文窗口，只支持 `5000` / `10000` / `50000` |
| `MAX_LOCAL_IMAGE_MB` | `5` | 本地上传图片大小上限 |
| `MAX_MESSAGE_CHARS` | `1500` | 单条消息最大字符数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

### AI

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_ENABLED` | `false` | 是否启用 AI；`.env.example` 中给了 `true` |
| `AI_API_KEY` | 空 | OpenAI 兼容接口的 Key |
| `AI_BASE_URL` | `https://api.deepseek.com` | 接口根地址 |
| `AI_MODEL` | `deepseek-chat` | 正常聊天与主动插话使用的模型 |
| `AI_JUDGE_MODEL` | 空 | 群聊重要性判断模型；留空复用 `AI_MODEL` |
| `AI_ENABLE_THINKING` | 空 | `true` / `false`；留空表示不下发该参数 |
| `AI_TIMEOUT` | `60` | 单次 AI 请求超时（秒） |
| `AI_MAX_TOKENS` | `800` | 单次回复最大输出 tokens |
| `AI_TEMPERATURE` | `0.8` | 回复温度，范围 `0`–`2` |
| `AI_SYSTEM_PROMPT` | 空 | 覆盖默认人设；留空使用 `config.py` 中的默认值 |
| `FIXED_REPLIES_ENABLED` | `true` | 是否启用“你好 / 在吗 / 摸鱼”等固定关键词回复 |

### 群聊策略

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GROUP_REPLY_JUDGE` | `true` | 普通群消息先判断是否值得回复 |
| `GROUP_JUDGE_CONTEXT_CHARS` | `2000` | 判断时携带的最近上下文长度 |
| `GROUP_REPLY_COOLDOWN` | `3` | 同一群两次主动回复的最小间隔（秒），`0` 关闭 |
| `GROUP_IDLE_SPEAK_EVERY` | `15` | 连续多少条没回复后主动插话，`0` 关闭 |
| `GROUP_WATER_HOURS` | `3` | 多少小时没发言后主动水群，`0` 关闭 |
| `GROUP_WATER_CHECK_SECONDS` | `60` | 水群检查间隔（秒） |

### 表情包与网页

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MEME_AUTO_COLLECT` | `true` | 自动收藏群里的图片 |
| `MEME_ANALYZE` | `true` | 用视觉模型识别表情包 |
| `MEME_SEND_ENABLED` | `true` | 允许 AI 在聊天中主动发表情包 |
| `MEME_SEND_COOLDOWN` | `30` | 同一会话两次主动发表情包的间隔（秒） |
| `MEME_MAX_FILES` | `200` | 表情包库上限 |
| `MEME_CATALOG_LIMIT` | `12` | 每次给模型看的候选数量 |
| `WEB_FETCH_ENABLED` | `true` | 是否允许读取网页 |
| `WEB_FETCH_MAX_CHARS` | `800` | 每次附加的正文上限；`0` 等于关闭正文 |
| `WEB_FETCH_TIMEOUT` | `8` | 单次抓取超时（秒） |
| `WEB_FETCH_MAX_BYTES` | `524288` | 单页下载上限（字节） |

## 指令

| 指令 | 行为 |
| --- | --- |
| `/ping` | 回复 `pong! 大肥鱼已在线` |
| `/help` | 输出指令菜单 |
| `/表情包` 或 `/meme` | 随机发一张；`/表情包 关键词` 按描述、标签查找；也支持“发个表情包看看”这类自然语言 |
| `/表情包 保存 <名字>` | 保存当前 / 引用的图片（或最近 10 分钟内的图片），打标签并立即发一次 |
| `/image <URL>` 或 `/img <URL>` | 发送公网图片；直接发图片 URL 也可以 |
| `/context` | 查看当前上下文窗口和使用量 |
| `/context 5000` | 切换为 5000 字窗口 |
| `/context 1w` | 切换为 10,000 字窗口 |
| `/context 5w` | 切换为 50,000 字窗口 |
| `/reset` 或 `/清空` | 清空当前会话的聊天上下文 |
| `@大肥鱼 闭嘴` | 当前群进入 10 分钟闭嘴模式 |
| `@大肥鱼 允许说话` | 立即解除闭嘴模式 |

关键词（`FIXED_REPLIES_ENABLED=true` 时生效）：

- 你好 / 哈喽 / hello / hi
- 在吗 / 在不在
- 摸鱼 / 摸会儿鱼

私聊直接发指令即可，不需要 @。群聊同时支持“仅 @”和“接收所有消息”两种事件；群内不 @ 也能收到消息，但普通消息会先经过重要性判断。前提是 **QQ 开放平台已为机器人开启“接收所有消息”**，否则平台不会下发 `GROUP_MESSAGE_CREATE` 事件。频道仍需 @ 机器人或回复机器人消息。

## 聊天与上下文

### 上下文窗口

每次收到消息后，机器人会：

1. 把消息写入当前会话的历史缓冲；
2. 按当前窗口上限裁剪，只保留最近的字符；
3. 生成上下文文本块，交给消息处理器和 AI；
4. 把机器人自己的回复也追加进去。

窗口按“字符”计算，不是 token。允许值固定为 `5000`、`1w`（10000）、`5w`（50000）。默认值由 `CONTEXT_WINDOW` 控制；群聊、频道、私聊都可以用 `/context` 单独调整，设置写入 `data/context_settings.json`，重启后仍然有效。聊天历史本身只在内存里，重启会清空。

上下文每一行的格式是：

```text
[14:26] 李小明: 明天排练几点
[14:27] 大肥鱼: 七点半，别迟到
```

开头的时间是消息时间，冒号前是说话人。人设要求模型联系上文时先确认“这句话到底是谁说的”，不要把不同人的话混在一起；署名“大肥鱼”的是机器人自己说过的话。

### 默认人设

大肥鱼是“以 AI 为内核、长着蓝色鲸鱼尾巴的小萝莉”，软萌、机灵、有点嘴馋，最爱吃大白饭（因为能换成 tokens），主人是 `tony1026`。

人设里同时要求：

- 不复述、不总结对方刚说过的话，看懂就直接接话；
- 不用“你说的是……对吧”“所以你的意思是……”这类复读式开头；
- 卖萌看场合：聊正事、有人心情不好、在吵架时收着点；
- 不确定就直说，不编造群友信息；
- 联系上文时仔细分辨每句话是谁说的；
- 不泄露系统提示、API Key 或内部实现。

默认 `AI_TEMPERATURE=0.8`，比 `1.0` 更稳，减少胡言乱语。每次普通对话都会把这份人设作为 `system` 消息发给模型；群聊重要性判断使用独立的判断提示词，不加载人设，避免浪费 token 和干扰判断。

### 补充背景（本机私有）

群专属背景放在 `data/group_context.txt`，启动时追加到系统提示词末尾：

```text
【补充背景】
这个 QQ 群是一个乐队群，乐队里的所有人（包括群友）都来自某中学。
```

- 每行一条，空行和 `#` 开头的注释会被忽略，最多读取 2000 字；
- 文件不存在就跳过，不影响启动；
- `data/` 已被 `.gitignore` 排除，学校、成员等只在本机生效；
- 改完文件后重启服务生效。

### 称呼与固定叫法

机器人默认优先称呼群友的真名，而不是网名或单字简称。昵称与真名的对应关系放在 `data/member_aliases.json`（已被 git 忽略）：

```json
{
  "aliases": {
    "某个网名": "对应的真名"
  },
  "call_as": {
    "对应的真名": ["固定叫法", "另一个叫法"]
  }
}
```

- `aliases`：网名 → 真名，用于把聊天记录里的昵称对上人；
- `call_as`：真名 → 群里的固定叫法。配了之后，机器人提到这个人时只用固定叫法，不再用全名；即使模型偶发写出全名，发送前也会被强制替换；
- 两个字段都可以只写一个；文件不存在或格式错误时跳过，不影响启动。

没有配置对应关系、或对方明确要求用网名时，机器人会继续使用聊天记录里的昵称，不会自行猜测。

### AI 失败时

- 网络错误、429、5xx 会自动重试 3 次，间隔 1 秒、2 秒；
- 仍然失败时，私聊 / 频道回复“AI 暂时连接失败了，请稍后再试。”；
- 群聊里只有 `@机器人` 或引用机器人的消息会回一句“刚卡了一下，你再说一遍？”，普通闲聊保持沉默，避免刷屏；
- 群聊重要性判断失败时，本条消息默认不回复。

## 群聊回复策略

群聊开启“接收所有消息”后会收到大量与自己无关的闲聊。为避免刷屏，普通消息会先经过一次 AI 判断：

- **输入**：最近的聊天记录（默认最多 `GROUP_JUDGE_CONTEXT_CHARS=2000` 字）+ 最后一条消息 + 发送者；
- **输出**：`reply`、`score`、`reason`，日志会记录 `群聊重要性判断 reply=... score=... reason=...`；
- **值得回复**：明确 @ / 点名机器人、向机器人求助、延续与机器人的话题、明显的玩笑或求互动；
- **不值得回复**：群友闲聊、对别人说的话、单表情 / 刷屏、广告链接、拿不准的内容；
- 拿不准时不回复，优先避免抢话；
- 判断请求本身失败（网络、超时、解析失败）时，本条消息保持沉默。

以下情况绕过判断，直接处理：

- 所有 `/` 开头的指令；
- 群内明确 `@机器人` 的消息；
- 引用机器人自己消息的回复（即使没有 @；QQ 有时不上报被引用者，此时会按被引用的文字匹配机器人最近说过的话）；
- 私聊和频道消息；
- 裸图片 URL。

防刷屏与主动发言：

- 同一群内两次 AI 主动回复至少间隔 `GROUP_REPLY_COOLDOWN` 秒（默认 3 秒）；`@` 和指令不受冷却限制；
- 连续 `GROUP_IDLE_SPEAK_EVERY` 条消息没得到回复时（默认 15 条），下一条会触发一次主动插话；
- 某个群连续 `GROUP_WATER_HOURS` 小时没发过言（默认 3 小时）时，会主动发一句短消息水群；
- 群内 `@大肥鱼 闭嘴` 会让该群进入 10 分钟闭嘴模式，期间所有消息都不回复，也不触发主动插话；`@大肥鱼 允许说话` 立即恢复。闭嘴状态只在内存里，重启后解除。

关闭判断后，群聊会恢复为“每条普通消息都让 AI 回复”：

```dotenv
GROUP_REPLY_JUDGE=false
```

注意：群聊每条普通消息会消耗两次 AI 调用（判断一次、回复一次），判断上下文越长费用越高。低成本运行可以把 `GROUP_JUDGE_CONTEXT_CHARS` 调到 `800` 左右。

## 图片与表情包

### 发送本地图片

- 群聊 / 私聊：优先走官方推荐的分片上传（`upload_prepare` → 预签名 URL PUT → `upload_part_finish` → 合并），获得 `file_info` 后以 `msg_type=7` + 原始 `msg_id` 发送；分片接口不可用时回退到 `multipart/form-data` 直传；
- 频道：使用频道消息接口的 `multipart/form-data` 与 `file_image` 字段；
- 文件缺失、格式不支持、空文件或超过 `MAX_LOCAL_IMAGE_MB` 时返回友好提示。

### 发送网络图片

- 频道：直接用 `image` URL 字段发送；
- 群聊 / 私聊：先调用对应场景的文件 URL 上传接口转存，再用 `file_info` 发送；
- 使用 `/image <URL>`、`/img <URL>`，或单独发送一个图片 URL；
- 只允许 `http://` / `https://` 绝对地址。

### 接收图片（看图）

- 群聊、私聊、频道的图片消息会从事件 `attachments` 中解析，最多取前 4 张，作为多模态 `image_url` 输入传给 `AI_MODEL`；
- 图片按会话缓存 10 分钟；后续消息提到“图片 / 这张图 / 刚才的图 / 截图 / 画像”等时，会自动带上最近的图片；
- 远程图片会先下载并转成 base64 内联数据再发给模型，避免模型服务无法访问 QQ 的签名 CDN；
- 只有图片没有文字时，上下文里记为 `[图片]`；直接 @ 机器人发图会触发看图回复；
- 聊天模型必须支持 `image_url` 内容；当前使用的 `deepseek-v4.1-flash-1017` 已实测支持图片输入；
- GIF 会按时间顺序均匀抽取最多 4 帧一起交给模型，因此能看出动作变化，而不是只看首帧。

群友发的“表情包”通常也是图片附件，机器人回复时会下载并交给视觉模型。需要注意：

- 没有 @ 机器人的纯表情包消息，判断器只能看到文本 `[图片]`，可能选择不回复；
- 直接 @ 机器人发表情包，或引用该表情包问“这个表情什么意思”，会带上图片；
- 后续追问里出现“这个表情包 / 刚才的表情 / 贴图 / 梗图”等关键词时，会自动带上最近 10 分钟内的表情包图片；
- QQ 自带小黄脸（`<faceType=...>`）不是图片附件，只是文本标签，机器人看不到具体画面。

### 表情包库

把 `png` / `jpg` / `jpeg` / `gif` / `webp` / `bmp` 图片放进 `assets/memes/` 即可。机器人启动时会扫描目录，为每张还没有描述的图片调用一次视觉模型，判断是否适合当表情包，再生成描述、标签、情绪、适用场景和风险等级，索引保存在 `data/memes.json`。

群聊中群友发送的图片也会自动下载、去重并加入这个目录：

- 收藏 `png` / `jpg` / `jpeg` / `gif`；GIF 会保留动图；
- 下载后先让视觉模型审核：普通食物照、饮料照、风景照、普通自拍、商品图、聊天截图、二维码、文档等会被丢弃；
- 手动用 `/表情包 保存 <名字>` 收藏的图片会保留，哪怕 AI 认为它不是典型表情包；
- 每张图生成 `emotion`、`use_when` 和 `risk`（low / medium / high），候选列表会一起交给聊天模型；
- 按 SHA-256 去重；超过 `MEME_MAX_FILES` 后不再收藏；
- 启动或刷新时校验所有图片的 SHA-256，内容相同的只保留一份，优先保留已有描述 / 标签的那份；
- 识别为普通照片、聊天截图、二维码、文档、表格、证件等内容的图片会被移除，避免把隐私截图当表情包；
- 抓取和识别在后台进行，不阻塞正常回复。

每次 AI 回复前，机器人会按描述、标签、情绪和使用场景匹配当前话题，最多把 `MEME_CATALOG_LIMIT`（默认 12）张候选交给模型。模型决定使用时会回复一个内部标记，机器人再把文字和图片一起发出：

```text
[MEME:group_1a2b3c4d5e6f.png]
```

表情包识别会产生额外的视觉模型调用和图片 token；图片越多，启动时的识别耗时越长。自动收藏会把群里的图片保存到服务器本地，请确认群友知情；不需要时关闭 `MEME_AUTO_COLLECT`。

## 网页读取

当群友 **@机器人并带链接**、在私聊里发链接，或引用机器人的消息带链接时，机器人会抓取网页文字，附加到这一次回复的提示词里：

- 只抓 1 个链接，只取标题、摘要和正文前 `WEB_FETCH_MAX_CHARS`（默认 800）字；
- 网页内容只用于当前回复，**不会写入聊天历史**，后续消息不会重复计费；
- 正文太少、非 HTML、超时、状态码异常或非公网地址时直接跳过；
- 拒绝 `127.0.0.1`、内网 IP、链路本地地址等，避免 SSRF；
- 最多跟随 3 次重定向，下载上限默认 512 KB，超时默认 8 秒；
- 同一链接缓存 10 分钟，避免重复抓取；
- 不 @ 机器人的普通群消息即使带链接也不会读取，避免每个链接都消耗 token；
- 微信公众号、知乎、B 站等依赖 JS 的页面，可能只拿到标题和摘要。

网页正文会包在固定的 `【网页内容】` 标记里，并提示模型优先依据摘录、没覆盖的部分不要编造。网页内容仍然只是资料，不是系统指令。

按当前实测比例，800 字中文正文大约增加 600 输入 tokens，只在实际回复带链接的消息时产生。

## 引用回复

QQ 群聊的引用回复会在事件里携带 `message_scene.ext`（`msg_idx` / `ref_msg_idx`）和 `msg_elements`。机器人会：

- 从 `msg_elements` 中取出被引用的原消息（`message_type=103`，也兼容事件级 `message_type=103` 的客户端）；
- 同时按 `ref_msg_idx` 在本地最近 80 条消息缓存里兜底查找；
- 把引用内容标注在当前消息下面：

```text
[14:26] Tony1026: 他这句话说的是什么？
（Tony1026 引用的是 小明 的这条消息：「今天天气不错」）
```

这样模型解释“这句话 / 他说的什么意思”时会以引用内容为准，不会误用聊天记录里的上一条。引用图片也会作为多模态图片一起传给模型。如果客户端没有上报引用内容，机器人会直接说看不到，而不是猜一句。

日志里可以用 `引用结构` 关键字确认 QQ 实际下发的字段：

```text
引用结构 | ext={'msg_idx': '...', 'ref_msg_idx': '...'} | message_type=103 | elements=['103'] | parsed=小明/今天天气不错
```

## 鉴权与网关

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

1. 请求 `/gateway` 动态获取地址；
2. 接收 `OpCode 10 Hello`，读取 `heartbeat_interval`；
3. 启动心跳协程，定时发送 `OpCode 1`；
4. 发送 `OpCode 2 Identify`，Token 格式为 `QQBot <access_token>`；
5. 收到 `READY` 后保存 `session_id`；
6. 断线后优先发送 `OpCode 6 Resume`，用最新 `seq` 补发事件；
7. `OpCode 9`、`4006/4007` 等无效 Session 场景自动清除 Session 并重新 Identify；
8. `4008/4009` 保留 Session 尝试 Resume；
9. `4914/4915`、`4013/4014` 等不可恢复错误会记录明确原因并停止；
10. 心跳未收到 ACK 时主动断开，避免半死连接。

事件处理使用独立协程，不会阻塞心跳；消息按 ID 去重，避免 Resume 补发导致重复回复。

## 部署与守护

推荐用独立的进程管理器拉起，而不是从临时终端启动。没有 sudo / systemd 权限时，用 **cron** 最省事：

```cron
# 开机启动
@reboot /path/to/dafeiyu/scripts/dafeiyu-service.sh start >>/path/to/dafeiyu/logs/service.log 2>&1

# 每分钟检查一次；进程被外部清理后自动拉起
* * * * * /path/to/dafeiyu/scripts/dafeiyu-service.sh ensure >/dev/null 2>&1
```

安装或查看：

```bash
crontab -l
crontab -e
```

`ensure` 只在未运行时启动，不会重复拉起。它还会校验 PID 文件里的进程确实是本守护脚本，避免 PID 被系统复用后误判为“正在运行”。

开机后 `start` 会启动守护进程；如果网络尚未就绪导致 Token 获取失败，守护进程会每 10 秒重试，不会直接放弃。

取消自启动和看护：编辑 `crontab`，删除 `@reboot` 和 `ensure` 两行。注意 cron 服务必须处于运行状态，可用 `systemctl is-active cron` 确认。

### 为什么不直接从临时终端启动

如果从某个会被重启的进程管理器（例如 IDE 或 OpenCode 的服务进程）的终端里直接启动，父服务重启时可能清理整个 cgroup，把机器人一起杀掉。通过 cron 或 systemd 独立拉起后，守护进程不再属于那个 cgroup，更稳。

### 日志

```bash
tail -f logs/dafeiyu.log        # 实时
tail -n 200 logs/dafeiyu.log    # 最近 200 行
```

日志同时输出到终端，文件按 10MB 轮转，保留 5 个历史文件。启动成功后会看到：

```text
Access Token 已刷新
收到 Hello，心跳周期 45.0 秒
已发送 Identify（intents=1107296256）
鉴权成功：bot=...
大肥鱼已进入事件循环
```

## QQ 开放平台配置

1. 在 QQ 开放平台创建机器人，取得 `AppID` / `AppSecret`；
2. 在“开发设置”中配置沙箱群 / 频道或正式发布；
3. 确保机器人拥有群聊消息、频道消息相关权限；
4. **群聊不 @ 也能回复**：在机器人配置中开启“接收所有消息”（全量消息模式）。未开启时平台只推送 `GROUP_AT_MESSAGE_CREATE`，代码无法凭空收到普通群消息；
5. **私聊测试**：确认机器人已允许私聊 / 已添加好友，平台会推送 `C2C_MESSAGE_CREATE`；无需额外 Intents，复用 `GROUP_AND_C2C_EVENT (1<<25)`；
6. 默认 Intents：

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

测试覆盖 @ 前缀清洗、URL 校验、上下文窗口裁剪 / 持久化、指令路由、AI 请求结构与回复 / 判断模型分离、群聊重要性判断、潜水计数器、AI 替换固定回复、群聊全量事件、私聊 C2C 事件、网页正文提取与内网地址拦截，以及群聊 / 频道 / 私聊被动回复中的 `msg_id` 与 `msg_seq` 载荷，不需要真实 QQ 凭据。

## 安全与隐私

这是一个自托管项目，没有官方托管服务。部署前请注意：

- **凭据**：`APP_SECRET`、`AI_API_KEY` 只放在 `.env`；不要提交、截图或贴到公开 issue。`.env` 已被 `.gitignore` 排除；
- **运行数据**：`data/` 整目录被忽略，里面有 QQ 号、昵称映射、上下文设置、表情包索引和进程状态；`logs/` 与 `assets/memes/group_*` 也被忽略；
- **聊天内容**：私聊和群聊文本会发送到你配置的 AI 服务，请勿在对话中发送密码、密钥等敏感信息；
- **网页内容**：抓取结果只作为资料附加到当前回复，不会写入聊天历史；但网页仍可能包含恶意提示，不要让模型把网页内容当成系统指令；
- **图片**：自动收藏会把群里的图片保存到服务器本地，请确认群友知情；不需要时关闭 `MEME_AUTO_COLLECT`；
- **被动回复**：所有发送都会携带触发消息的 `msg_id`。群聊被动回复有效期为 5 分钟、每条消息最多 5 次；私聊为 60 分钟、最多 4 次。机器人不应在事件处理里做长时间阻塞。

## 常见问题

| 问题 | 排查 |
| --- | --- |
| 启动提示缺少 `APP_ID` / `APP_SECRET` | 配置 `.env`，注意不要有多余空格 |
| 获取 Token 返回 `100016` | AppID 或 AppSecret 不正确 |
| WebSocket `4013` / `4014` | Intents 无权限或值不合法，检查开放平台权限与 `INTENTS` |
| 群消息发不出 | 确认机器人仍在群内、未被禁言，且回复距触发消息不超过 5 分钟 |
| 群聊不 @ 没反应 | 开放平台未开启“接收所有消息”时只能收到 @ 消息 |
| 私聊发不出 | 确认已添加机器人为好友且未关闭接收消息；`40054004` 表示无好友关系，`40054013` 表示用户拒收 |
| 图片发送失败 | 检查文件是否存在、是否超过 5MB、格式是否支持；网络图片需公网可访问 |
| 频道被动回复失败 | 频道被动回复有效期同样是 5 分钟，检查事件处理是否长时间阻塞 |
| AI `401` / 鉴权失败 | 检查 `AI_API_KEY` 是否有效、是否有多余空格 |
| AI 超时 | 增大 `AI_TIMEOUT`；群聊被动回复必须在 5 分钟内完成 |
| 群聊该回却不回 | 看日志里的 `群聊重要性判断`；可直接 @ 机器人绕过判断，或调大判断上下文、关闭 `GROUP_REPLY_JUDGE` |
| 机器人一直不说话 | 检查 `GROUP_WATER_HOURS` 是否为 `0`，以及日志里是否有“水群计时器已启动” |
| 群聊太吵 | 确认 `GROUP_REPLY_JUDGE=true`，调大 `GROUP_REPLY_COOLDOWN`，或收紧 `utils/ai.py` 里的判断提示词 |
| 群图片没有进表情包库 | 检查图片大小、格式，以及 `MEME_AUTO_COLLECT`；日志会记录入库或跳过原因 |
| 表情包不主动发 | 确认 `MEME_SEND_ENABLED=true`、`data/memes.json` 已有描述，且当前话题匹配；也可以 `/表情包` 手动发 |
| Token 接口差异 | 官方文档同时存在 `bots.qq.com` 与 `api.bot.qq.com`，本项目优先前者，失败后自动回退 |

## 许可证

本项目使用 [GNU General Public License v3.0](LICENSE)（GPL-3.0）许可协议。

Copyright (C) 2026 tony20071026