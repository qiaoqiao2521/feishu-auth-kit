# Telegram 最小文本试点

目标：陌生开发者用一个新 Bot，把 Telegram 的一条普通文本交给官方 Chat SDK，
收到 `Echo: 原文`。不是 Agent 平台，也不接管既有飞书入口。

**当前结论：已完成可运行入口和隔离 HTTP 回归；真实 Bot 往返尚未验收。**
本轮没有可用 Bot 凭据，也没有配置公网 HTTPS 地址。以下真实操作由持有新 Bot
凭据和公网入口的开发者执行。不要把离线测试或 healthz 的 200 当作真实往返证据。

## 1. 从干净目录安装

要求 Node.js 24、pnpm 11.25.0；从仓库源码运行，需安装开发依赖。
不要用 `--prod` 安装，也不要在飞书生产部署目录切分支或启动。

```bash
git clone --branch feat/channel-kit-migration https://github.com/qiaoqiao2521/feishu-auth-kit.git telegram-pilot
cd telegram-pilot
pnpm install --frozen-lockfile
pnpm test:telegram
cp examples/chat-sdk/telegram.env.example .env.telegram
chmod 600 .env.telegram
```

所有命令均在仓库根目录运行。`chat`、Telegram adapter、memory state 保持锁定
`4.43.0`；本补丁没有新增依赖。`.env.telegram` 已忽略，勿提交真实 token。
Node 的 `--env-file` 不覆盖已有进程环境变量；若机器已有同名变量，先在干净终端
核对其来源。CLI 不读取 `TELEGRAM_API_BASE_URL`，不会把 token 发到自定义 API 地址。

## 2. 创建专用 Bot，填写身份和授权

向 [BotFather](https://t.me/BotFather) 发送 `/newbot`，把它给出的 token 和 bot username
填入 `.env.telegram`。不要复用已有生产 Bot。生成 webhook secret：

```bash
node -e "console.log(require('node:crypto').randomBytes(32).toString('hex'))"
```

把结果填入 `TELEGRAM_WEBHOOK_SECRET_TOKEN`。配置必须同时具有 token、username、secret
和非空 grants，禁止空授权列表或通配授权。

在 Telegram 私聊新 Bot，点击 Start，再发送普通文本 `identify me`。在注册 webhook
**之前**执行（此时 grants 仍可暂用模板值，ids 命令仅检查身份）：

```bash
pnpm telegram:ids
```

输出仅含最近消息的 `userId`、`chatId` 和可选 `topicId`，不打印消息正文或 token。
把自己的那一条填入 `.env.telegram` 的 `TELEGRAM_GRANTS`，例如：

```dotenv
TELEGRAM_GRANTS='[{"userId":"12345678","chatId":"12345678"}]'
```

不要照抄示例 ID。`ids` 不传 offset、不确认消费更新；旧的识别文本可能在注册 webhook
后收到一次 Echo，这是预期行为。若输出 `[]`，先确认消息发给正确的 Bot；该步骤
最多查看 100 条最近排队更新，适用于新 Bot。已有 webhook 会使该命令拒绝操作，
不会自动删除 webhook。若要测试群话题，先让 Bot 加入测试群，在目标话题里 @Bot
发一条文字，再用同一命令取得用户、群和话题 ID；grants 三者必须匹配。

## 3. 启动本地入口

```bash
pnpm telegram:check
pnpm telegram:start
```

启动前调用 `getMe`，身份与账号/username 不符、凭据被拒或网络不可达时退出。
成功会输出 `listening`，默认地址 `127.0.0.1:8787`，不会启动 polling 或自动注册 webhook。
另一个终端检查：

```bash
curl --fail http://127.0.0.1:8787/healthz
```

预期包含 `state: memory`、`realRoundTrip: unverified`。这是进程探活，不是 Telegram
收发探测；即使后续完成真实往返，这个静态字段也不会自动改成 verified。

## 4. 提供 HTTPS 并显式注册 webhook

服务需能出站访问 `https://api.telegram.org`；Telegram 必须能入站访问有效 TLS 的公网
HTTPS 地址。仅 localhost 或局域网地址不够。可使用自己已有的 HTTPS 反向代理或隧道。
在**试点自己的域名/代理配置**中转发到本地服务；不要覆盖飞书站点配置。例如 Nginx
现有 HTTPS server 内的独立 location（TLS 证书和域名由宿主提供）：

```nginx
location = /webhooks/telegram {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header X-Telegram-Bot-Api-Secret-Token $http_x_telegram_bot_api_secret_token;
    proxy_read_timeout 20s;
    client_max_body_size 64k;
}
```

该片段不是完整 TLS 配置。公网入口路径固定为 `/webhooks/telegram`，应保留 POST body、
Content-Type 和 secret header，不应缓存响应。如果代理在另一容器，需明确设置
`TELEGRAM_HOST` 和容器端口映射；默认绑定只允许同机代理访问。

把实际地址写入 `.env.telegram` 的 `TELEGRAM_WEBHOOK_URL`，例如
`https://pilot.example.com/webhooks/telegram`，然后在另一个终端执行：

```bash
pnpm telegram:register
pnpm telegram:check
```

`register` 是明确的 Bot 配置写操作：仅允许 message 更新、连接数为 1，不丢弃积压更新；
遇到不同的已有 webhook 拒绝覆盖。启动/退出服务均不注册、不删除 webhook。
check 输出身份是否验证、是否已注册 webhook、积压条数及是否有历史投递错误；不打印原始
上游异常。若有错误，先查进程是否仍在、HTTPS 路由是否正确和 secret 是否一致。

## 5. 完成一次真实验收

用已授权用户，在已授权私聊发送新的普通文本 `pilot-acceptance-001`。
预期在 Telegram 客户端看到 `Echo: pilot-acceptance-001`。只回复前 1000 字符。
首次点击 Start 的 `/start`、命令、附件、相册、按钮和编辑事件都不在此试点范围；
它们会被忽略，不应拿 `/start` 无回复来判断文本链路失败。

验收时记录：代码 commit、SDK 版本、时间、Bot username、授权 user/chat/topic ID、
发出文本与收到的 Echo，以及对应 `webhook_completed` 日志的 updateId。不要记录 token、
secret 或完整原始 update。真实往返以客户端看到对应回复为准，不能只看 webhook 200。

再用未授权用户发送消息，预期无回复、无“正在输入”，服务有 `denied` 事件。
修改权限后需重启服务；如果更改 secret，需同时重新执行 register。
关停按 Ctrl+C；webhook 会保留，Telegram 可能积压/重试，不代表进程仍在运行。
如不再使用试点，请在 BotFather 撤销这个专用 Bot 的 token。

## 可重复的隔离回归与已知边界

```bash
pnpm test:telegram
pnpm test
pnpm typecheck
pnpm typecheck:chat
pnpm build
pnpm test:package
```

`tests/telegram-server.test.ts` 在动态 loopback 端口运行真实 HTTP 入口与假的 Bot API 服务，
SDK 本身未替换。另用独立子进程执行原始 CLI，验证 env 文件、启动、文本往返与 SIGTERM 退出；
仅将出站网络改向本地假的 Bot API。测试不接触真实平台、不需要真实 token，不会注册真实 webhook。

| 情况 | 当前行为 / 验收上限 |
| --- | --- |
| secret 缺失/错误 | 401，不调用 SDK 或出站 API |
| user/chat/topic 或 bot 身份不匹配授权 | 返回 200 `denied` 以终止无意义的重试，业务拒绝、没有出站调用 |
| 非法 JSON/字段、过大请求体 | 400 / 413；不发送消息 |
| 授权文本 | SDK 接收并发送 Echo；隔离环境已复验 |
| 同进程重复/同时投递相同 update_id | SDK 去重，仅一次 sendMessage |
| SDK 发送被拒、网络断开 | 502 `reply_failed_or_unknown`；不自动重发，日志不含原始异常 |
| handler 超过 15 秒 | 502，结果未知；后台任务可能仍然完成，不能据此另起一次发送 |
| 失败后重复相同 update_id | 去重可能返回 200 且没有重新发送；**没有恢复成功保证** |
| 新建 runtime 后重复相同 update_id | 可再次发送，证明内存去重不跨 runtime 保留 |
| 进程被杀/跨进程恢复 | **尚未验证，也没有实现持久账本或恢复队列** |
| 真实 Telegram 往返、群话题、外部 TLS 路由 | **尚未验证，需要实际凭据和公网入口** |

SDK 的 webhook 去重和消息去重均早于业务成功；持久 state 本身也不能保证消息恢复。
因此不要把 502 后的 Telegram 自动重投当作可靠补偿，更不要宣称 exactly-once。
这是单实例、短文本、少量人工验收用的试点，不适用于重要投递或多实例生产运行。
下一步应先拿到一条真实往返证据，再决定是否补官方持久 state 和业务投递记录。

本轮（2026-11-03，上海时间）实际执行：TypeScript 190 项、Python 203 项通过；
TypeScript 两项类型检查、构建和包入口检查通过。190 项中本补丁新增 29 项 HTTP/配置回归。
没有修改 `src/`、Python 实现、已有凭据或飞书服务配置。

实现依据：锁定依赖包内的 `dist/index.js` / 类型声明，以及
[Chat SDK Telegram 文档](https://chat-sdk.dev/adapters/official/telegram)、
[Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)。官网可能随上游更新，
版本行为以本仓 lockfile 对应发布包和回归结果为准。
