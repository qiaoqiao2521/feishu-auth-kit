# 渠道复用评估：先接现有 adapter

核对日期：2026-10-31。跨渠道接入优先复用已有 transport / adapter；本 kit 补授权、身份隔离、runner 和交付记录等宿主契约。目录中列出某个平台，只能证明发现了候选，不能证明版本兼容、重连可靠或已经真实收发成功。

本轮读取的固定源码、npm 元数据、实际下载包哈希见 [reuse-sources.lock.json](../upstream/reuse-sources.lock.json)。原有腾讯官方 QQ / iLink 协议证据仍见 [sources.lock.json](../upstream/sources.lock.json)，二者分别保存。

## 候选与版本

Vercel 的 [adapter 目录源码](https://github.com/vercel/chat/tree/6ce9852ddb0ebd4e122435fafd7dfda8431243ff/apps/docs/content/adapters) 将 Lark 列为 vendor-official，将 QQ、WeCom、Weixin 列为 community。这里的 official 是维护主体分类，不能混同为所有组件都由 Vercel 维护。

| 候选 | 维护 / 平台身份 | npm 发布观察 | 直接依赖与 Node 要求 | 本轮结论 |
| --- | --- | --- | --- | --- |
| `@larksuite/vercel-chat-adapter` | 飞书供应商 adapter，Chat 目录为 vendor-official | `0.3.0`，2026-09-14 | `@larksuite/channel: 0.7.1`、shared `^4.28.1`；peer chat `^4.26.0`；Node `>=20` | 可优先复用飞书收发 / stream，薄层接入本 kit 的身份与 runner |
| `@agentor/chat-qq` | 社区维护，调用 QQ Bot 开放平台 API；不是个人 QQ 登录 | `0.1.1`，2026-07-28 | shared `4.35.0`；peer chat `^4.0.0`；未声明 engines | 已有 WS / callback / 媒体实现；需补接入边界测试，不另造 QQ 协议栈 |
| `@agentor/chat-wecom` | 社区维护，企业微信；不是个人微信 iLink | `0.1.3`，2026-08-09 | shared `4.35.0`；peer chat `^4.0.0`；未声明 engines | 分群 webhook、智能机器人、企业应用分别评估，不能当成同一收发能力 |
| `chat-adapter-weixin` | 社区维护，直接调用微信 iLink Bot HTTP API | `0.1.4`，2026-07-20 | shared `^4.31.0`、qrcode-terminal `^0.12.0`；peer chat `^4.31.0`；Node `>=22`；silk-wasm 可选 | 可复用已有 StateAdapter / 轮询 / 媒体，但须先处理与当前官方源码的协议差异 |
| 腾讯 `openclaw-qqbot` | 腾讯官方插件；OpenClaw 宿主集成 | 固定源码 package.json 为 `2.0.4`，未在本轮认定为安装版本 | connector `1.2.0`、qqbot-nodejs `^1.0.4` | 官方协议 / SDK 基线；OpenClaw 插件整体不等于通用 Chat adapter |
| 腾讯 `openclaw-weixin` | 腾讯官方 iLink 插件；OpenClaw 宿主集成 | 固定源码 package.json 为 `2.4.10`，未在本轮认定为安装版本 | 原锁文件记录其 package.json | 作为当前登录、ID、回复上下文和 CDN 协议的比较基线 |

shared 指 `@chat-adapter/shared`。本轮 registry 的 chat latest 为 `4.43.0`（2026-10-19，Node `>=20`）；这不是要求所有项目盲目升到 latest。符合 peer range 也不代表每个较新的 Chat 方法都被旧 adapter 实现。

版本日期是 registry 的该版本发布时间。Agentor 当前源码 HEAD 为 `53a754658096a97a373ccb09a505f0cddb788b63`（2026-08-09），Weixin 为 `62a2b42a26b48f731ef585114017669fccf95ca5`（2026-07-20），仓库均未归档。这些是维护观察，不足以判定成熟、无人维护或生产可用。

## 证据分层

| 证据 | 本轮实际完成 | 能说明什么 |
| --- | --- | --- |
| 目录 / README | 读取 Vercel 分类和各仓库说明 | 候选归属、声明能力、配置入口 |
| 固定源码 | 读取 25 个文件，核对 Git blob SHA-1 并计算 SHA-256 | 指定提交中的实际分支和状态保存方式 |
| npm 发布包 | 下载上述 4 个 adapter 的确切 tarball，验证 registry SHA-512 integrity，计算 tarball / 文件 SHA-256；未执行安装脚本 | 声明的 npm 版本确实包含已审阅的构建文件 |
| 本地运行证明 | Node 24.19.0，Chat / memory 4.43.0，Lark 0.3.0、QQ 0.1.1、Weixin 0.1.4；3 项真实发布包导入 / 构造测试和 typecheck 通过 | 未调用 initialize / connect；只证明这些 exports、构造及类型组合，未证明收发或重连 |
| 真实平台 | 本轮没有扫码、连接或发消息 | 尚不能承诺真实账户可用 |

`@larksuite/vercel-chat-adapter@0.3.0` 的 npm repository 指向 `larksuite/vercel-chat-adapter`，本轮 GitHub 查询返回 404，未杜撰该版本源码 commit。它的已核对证据是确切 npm 构建包；[node-sdk 中较早的 adapter 文档](https://github.com/larksuite/node-sdk/blob/b188be60134d409b61259546bcf69c731d2b0b7c/docs/vercel-chat-adapter/README.md) 是另一层资料。当前发布包已改依赖 `@larksuite/channel`，不能把旧文档的 node-sdk 依赖关系照搬到 0.3.0。

Agentor 的 npm 元数据未提供 gitHead，版本号相同不证明当前仓库 HEAD 就是该 tarball 的构建提交；锁文件分别固定仓库和确切发布包。Weixin registry gitHead 则与上述固定 HEAD 一致。旧 `wong2/weixin-chat-adapter` 地址跳转到规范仓库 [wong2/chat-adapter-weixin](https://github.com/wong2/chat-adapter-weixin)。

本地候选验证命令为 `pnpm exec vitest run tests/chat-sdk-candidates.test.ts`（3 / 3）与 `pnpm typecheck`。测试在动态导入之前设置拒绝 HTTP / WebSocket 的替身，并确认没有计时器被创建；未初始化 adapter。WeCom 本轮只核对发布包和源码，没有安装 / 构造通过记录。Telegram 的真实 SDK 假 transport 测试另由主任务记录，不据此提高 QQ、微信或飞书的 proof 等级。

## 需要薄层处理和验证的边界

### 飞书

0.3.0 构建文件实际调用 `@larksuite/channel` 的 connect / disconnect / send / stream，不需要再写一套长连接。adapter 只提供 WebSocket；`handleWebhook()` 返回 501。它导出 `registerLarkApp`，实际转发 channel 的 `registerApp`。

thread ID 为 `lark:<chatId>:<rootId>`；原生 `omt_*` topic container 不能冒充回复消息 ID。DM 检测依赖内存中的 p2p chat cache；应用配置是单 app。薄层仍需把 app / account 命名空间加到本 kit 授权与状态键中，不能仅凭 thread ID 推定发送者身份或多账户隔离。

README 将消息去重 / 锁交给 Chat state；已读构建文件显式将 stale window 放大、关闭 chat queue 和 batch。应明确哪个宿主负责去重和排队，避免叠加两套会丢消息的过滤。SDK 的重连、限流处理也不能替代本地发送 intent / unknown 结果记录。

### QQ

[bot.ts](https://github.com/DemoMacro/agentor/blob/53a754658096a97a373ccb09a505f0cddb788b63/packages/chat-qq/src/adapters/bot.ts) / [bot-websocket.ts](https://github.com/DemoMacro/agentor/blob/53a754658096a97a373ccb09a505f0cddb788b63/packages/chat-qq/src/adapters/bot-websocket.ts) 已包含 token 缓存、heartbeat、指数退避和 session resume。session ID、sequence、最近入站 msgId 与回复 msg_seq counter 是内存状态；不能由“支持自动重连”推导出进程重启后恢复或发送幂等。thread ID 包含 mode / scene / target，没有 appId，宿主需显式加账户作用域。

已读源码和确切 0.1.1 包的 callback 分支仅在 signature 与 timestamp 都存在时执行验签。公共 ingress 必须先要求这些头并验证失败即拒绝，或先限制为受控 WS 试点；不能把“有验签函数”当成缺失签名也会被拒绝。需用本地回放验证缺头、坏签名、跨 app / scene、重放和停止期间的重连行为。

媒体有多场景分支和不支持时的文本退化。复用这些能力后仍需为被动回复关联、msg_seq、平台额度、媒体上传后发送失败分别记录结果；超时后不能只因上游抛错就自动重试。

### 企业微信

[chat-wecom adapters](https://github.com/DemoMacro/agentor/tree/53a754658096a97a373ccb09a505f0cddb788b63/packages/chat-wecom/src/adapters) 有三条不同路径：群 webhook 单向推送、智能机器人 callback / WS、企业应用 API。不要用其中一条的 proof 覆盖另外两条，也不要把 WeCom 能力当成个人微信支持。

群 webhook 的原生 thread ID 直接包含 webhook secret key；回复返回的 ID 可以由 `Date.now()` 生成。薄层不能把这些原生标识写入普通消息 envelope / 日志，也不能将本地生成的 ID 当成服务端投递回执。智能 callback 的 response_url、WS 待响应请求是内存关联；重启、连接断开和超时都需要外部账本处理未知结果。

callbacks 的签名 / AES 能力、template-card、媒体与 token 逻辑已经存在，应复用并测试；试点授权、账户路由、敏感标识映射及交付状态仍属于宿主。

### 微信 iLink

[adapter.ts](https://github.com/wong2/chat-adapter-weixin/blob/62a2b42a26b48f731ef585114017669fccf95ca5/src/adapter.ts) 按 account / user 保存 context token，处理批次后更新 get_updates_buf，并支持 media 与 typing。它确实提供了可复用的客户端，而不是只有目录条目。StateAdapter 的持久性和宿主多 consumer 行为仍需单独证明；memory state 不会让游标和上下文跨进程持久化。

与 [2026-10-13 腾讯官方源码基线](weixin.md) 的已观察差异：

- 社区 0.1.4 获取二维码使用 GET；官方当前源码使用带 local_token_list 的 POST。
- 社区登录分支覆盖 redirect / expired / confirmed；官方还有验证码、验证码阻断和已绑定状态。
- 社区 types 将 message_id 设为 number，响应使用普通 JSON.parse；官方当前已做 uint64 ID 无损字符串解析。
- 社区 sendMessage 返回 void，adapter 以本地 client_id 形成结果；这不是服务端消息 ID，也没有证明 client_id 会被服务端幂等处理。

这些是源码不一致，尚未向真实服务验证 GET 或旧状态机是否仍被兼容。优先给既有 adapter 提交兼容修复或做明确锁定的补丁，再核对 current 官方 fixture；不应为绕过差异复制另一套完整 iLink 实现。发送 context token 必须绑定正确 account / user，媒体多请求结果与超时未知状态分别保留。

## Satori 与其他生态

[Satori](https://satori.chat/zh-CN/introduction.html) 是可评估的统一服务 / sidecar 路线：现成平台 adapter 在独立进程运行，本 kit 通过 HTTP / WebSocket 接入。官方 [事件协议](https://satori.chat/zh-CN/protocol/events.html) 的 IDENTIFY token / sn、READY 与 heartbeat 支持认证和断线恢复；[API](https://satori.chat/zh-CN/protocol/api.html) 用 Satori-Platform / Satori-User-ID 标明平台和账户。代价是新增服务部署、服务凭证、账户路由和 sn 持久化；恢复序号不等于发送 exactly-once。

[Satori QQ adapter](https://satori.chat/zh-CN/adapters/qq.html) 使用 QQ 开放平台 AppID / Token / Secret，不能与个人 QQ 协议实现混同。Koishi、NoneBot 等按已有部署和具体 adapter 再评估；不会因为“平台很多”就整体安装，也不会把个人协议、官方 bot API、WeCom 和 iLink 混成一个能力。

本阶段取舍是先用确切版本证明可导入、可构造、可在假 transport 中归一化和发送，再接宿主授权 / runner / 状态。只有真实测试账户往返、重启、拒绝和未知发送结果都有记录后，才提高平台就绪等级。
