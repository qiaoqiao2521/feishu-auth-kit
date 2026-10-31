# QQ Bot：源码观察与待实现边界

来源：[tencent-connect/openclaw-qqbot](https://github.com/tencent-connect/openclaw-qqbot/tree/89988812442583bf47c34b89f9abf5fd4b85df0b)，提交 `89988812442583bf47c34b89f9abf5fd4b85df0b`，提交日期 2026-10-12，核对日期 2026-10-31。package.json 声明插件版本 `2.0.4`。文件哈希见 [锁文件](../upstream/sources.lock.json)。

本 kit 当前没有 QQ adapter。以下是官方插件的客户端行为观察，未进行 QQ 账户登录或收发验收。

## 登录与凭证

[`src/setup/login.ts`](https://github.com/tencent-connect/openclaw-qqbot/blob/89988812442583bf47c34b89f9abf5fd4b85df0b/src/setup/login.ts) 接受直接配置 `appId:clientSecret`，或通过 `@tencent-connect/qqbot-connector` 的 `startQrConnect()` 获取二维码和凭证。二维码流程分 start / wait 两阶段；成功回调可返回多个账户的 `appId`、`appSecret` 和扫码用户标识。

插件声明 connector 为 `1.2.0`、`@tencent-connect/qqbot-nodejs` 为 `^1.0.4`。本轮未读取这两个 npm 发布包的内部协议，不能从插件包装层推断二维码服务 URL、token 刷新或 WebSocket 握手的完整契约。

## 传输与事件

[`src/gateway/qqbot-gateway.ts`](https://github.com/tencent-connect/openclaw-qqbot/blob/89988812442583bf47c34b89f9abf5fd4b85df0b/src/gateway/qqbot-gateway.ts) 将账户配置交给 SDK，默认 REST 基址为 `https://api.sgroup.qq.com`，token 基址为 `https://bots.qq.com`。完整 gateway 把 `transport`、停止信号和按账户保存的 session store 交给 SDK；send-only 实例仅用于发送。不能据此宣称本 kit 已有连接、resume 或重放去重能力。

[`src/adapter/webhook.ts`](https://github.com/tencent-connect/openclaw-qqbot/blob/89988812442583bf47c34b89f9abf5fd4b85df0b/src/adapter/webhook.ts) 使用 SDK 的 `verifyWebhookSignature` 和 OpenClaw webhook ingress，按账户 secret 进行 Ed25519 签名匹配。新宿主需自行提供经过验证的 HTTP ingress；请求 JSON 中自称的账户或用户字段不能代替传输认证。

## 回复上下文与媒体

[`src/gateway/event-handlers.ts`](https://github.com/tencent-connect/openclaw-qqbot/blob/89988812442583bf47c34b89f9abf5fd4b85df0b/src/gateway/event-handlers.ts) 使用 SDK 的 `replyTarget.scope` 区分 C2C 与 group，分别保留目标 ID、发送者 ID、账户 ID 和入站 `messageId`。发送包装器通过 `msgId` 绑定被动回复；没有显式值时会查本地缓存。

`src/outbound/target.ts` 的目标字符串归一化接受 `channel:`，但其 `parseTarget()` 只显式处理 C2C / group。不能把目标字符串可解析视为频道收发已经覆盖。

`src/outbound/reply-limiter.ts` 的本地默认值为同一消息 4 次回复、1 小时 TTL，并建议超限后转主动消息。这是插件策略，不是本轮核实过的服务端额度。新 adapter 应把“无被动回复上下文”暴露为能力限制；自动转主动发送需要单独的产品和权限决定。

`src/outbound/media-send.ts` 把图片、语音、视频和文件交给 SDK，保留 `replyToId`；语音失败分支可再尝试发文件。上传协议、媒体限制和转码仍属于未核对的 SDK 边界。发送超时后是否已投递不能由本地超时判定，不能据此安全重发或切换媒体类型。

## 后续验收

1. 锁定并实际校验 connector / nodejs 发布包，读取 token、连接恢复、签名和媒体实现。
2. 用脱敏事件回放证明账户、C2C / 群、发送者和回复 ID 不混用；验证重复事件和重连重放。
3. 实现按账户隔离的凭证与 session 持久化，并验证接收停止后不会继续发送。
4. 在授权测试账户中完成二维码登录、接收、一次被动回复、重连、额度拒绝及媒体验收，分别记录成功回执和未知投递结果。
