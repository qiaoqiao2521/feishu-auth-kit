# Telegram Bot API：已核对的协议边界

核对日期：2026-10-31。来源为 [官方 Bot API](https://core.telegram.org/bots/api) 与方法 / 类型文档；[changelog](https://core.telegram.org/bots/api-changelog.md) 最新列出的版本为 **Bot API 10.3（2026-08-24）**。精确页面内容哈希见 [锁文件](../upstream/sources.lock.json)。

文档通过只读 HTTPS 获取。Firecrawl 对该站返回不支持站点，随后使用普通 HTTPS GET 读取官方页面。没有调用带 bot token 的线上 API。本地实现与测试的范围见 [迁移说明](../docs/channel-kit-migration.md)。

## 鉴权与响应

请求使用 `https://api.telegram.org/bot<token>/<method>`。token 进入 URL，错误信息和日志不能原样打印该 URL。响应 `ok: true` 时读取 `result`；失败读取 `description`、`error_code` 和适用的 [ResponseParameters](https://core.telegram.org/bots/api/types/responseparameters.md)，例如以秒为单位的 `retry_after`。

文档规定错误响应的 `error_code` 内容可能变化。网络超时也不能证明发送失败；本地保存的发送 intent 和未知结果处理属于本 kit 的恢复策略，并非 Telegram 提供的 exactly-once 协议。

## 接收与游标

[getUpdates](https://core.telegram.org/bots/api/methods/getupdates.md) 与 webhook 互斥，平台最多保留 24 小时更新。getUpdates 的正 `timeout` 以秒计；`limit` 为 1–100，默认 100。更新在下一次调用传入 `offset > update_id` 时被确认。

因此调用方应先保存批次 receipt，再推进可恢复的处理状态和 offset。默认启动不能用 `drop_pending_updates` 或负 offset 丢弃待处理更新。进程关闭、处理异常或授权拒绝都应有明确游标策略；当前实现行为由本地测试证明。

`allowed_updates` 省略时复用此前设置；它不改变调用前已创建的更新，解析器仍需拒绝或显式忽略不支持的类型。平台的 `update_id` 可用于去重，但不能把它当成发送幂等键。

[setWebhook](https://core.telegram.org/bots/api/methods/setwebhook.md) 可设置 `secret_token`，请求携带 `X-Telegram-Bot-Api-Secret-Token`。将来如增加 webhook host，需要独立验收 secret 校验、持久化接收后的 HTTP 应答与重复投递；当前 polling host 不提供该能力。

## 回复上下文与媒体边界

[sendMessage](https://core.telegram.org/bots/api/methods/sendmessage.md) 要求 `chat_id` 与解析实体后 1–4096 字符的文本。以下字段语义独立：

| 字段 | 作用 |
| --- | --- |
| `reply_parameters.message_id` | 回复的原消息 |
| `message_thread_id` | forum / 启用 topic 模式的私聊主题 |
| `direct_messages_topic_id` | channel direct messages 的目标主题 |
| `business_connection_id` | 代发的 business connection |

[ReplyParameters](https://core.telegram.org/bots/api/types/replyparameters.md) 还有跨聊天引用、quote 和 ephemeral 等限制；普通文本试点不能默认接收这些扩展权限。群消息的发送者、聊天 ID、bot ID、topic ID 与账户凭证必须分别保存并纳入授权范围。

媒体有三种路径：复用当前 bot 的 `file_id`、平台从 URL 获取、multipart 上传。具体媒体限制随方法而异；官方当前 [getFile](https://core.telegram.org/bots/api/methods/getfile.md) 对云端下载列出 20 MB 上限，下载 URL 至少有效 1 小时。URL 含 token，不能作为普通可分享媒体 URL。local Bot API server 有不同限制，当前研究不把它与公共云端 API 混为同一部署。

本轮客户端提供 getMe、getUpdates、文本 send / reply / [edit](https://core.telegram.org/bots/api/methods/editmessagetext.md)、[callback query 应答](https://core.telegram.org/bots/api/methods/answercallbackquery.md)，以及需显式启用的 [sendMessageDraft](https://core.telegram.org/bots/api/methods/sendmessagedraft.md) 预览。归一化器保留文本 / caption 和不透明的 file_id 附件引用；它不下载附件，也没有媒体发送接口。官方 draft 面向 private chat，是临时 30 秒预览，成功只返回 true；最终仍须 sendMessage 才持久化，不能替代最终消息回执。

这些方法不代表已实现完整交互按钮流程、business / ephemeral / direct-message topics 或所有 Telegram update 类型。新增能力必须更新 capability 声明、fixture 和平台验收记录。

## 后续真实验收

使用授权测试 bot 完成 `getMe` 身份核对、首次接收、作用域内回复、未授权用户拒绝、重启游标恢复和限流响应；随后在可控网络中核对超时后的未知状态。媒体与 webhook 单独验收。本轮没有执行这些线上步骤。
