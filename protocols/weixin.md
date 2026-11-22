# 微信 iLink：源码观察与待实现边界

来源：[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin/tree/874637a0dbc24050f72d1385add4fc6170a2d726)，提交 `874637a0dbc24050f72d1385add4fc6170a2d726`，提交日期 2026-10-13，核对日期 2026-10-31。package.json 声明版本 `2.4.10`。文件哈希见 [锁文件](../upstream/sources.lock.json)。

本 kit 当前没有在线微信接入。`chat-adapter-weixin@0.1.4` 是开发评估依赖；已有的 [Python 离线文本边界](../src/feishu_auth_kit/weixin_ilink.py) 不包含登录、网络收发或持久恢复。官方 [protocol_zh_CN.md](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/docs/protocol_zh_CN.md) 明确指出：文档根据客户端代码编写，类型字段可选不代表服务端接受省略，也不保证类型中列出的能力已实现。以下保留这个证据边界。

## Issue #17：owner 与 context token 离线复核（2026-11-22）

本节补充 [Issue #17](https://github.com/qiaoqiao2521/feishu-auth-kit/issues/17)，不改写下方 2026-10-13 历史基线。审查本仓 `8853f90c6b78a8e01a75f812245f62a86d77442f`，保持 Chat `4.43.0` 和社区 adapter `0.1.4` 不变。

- 腾讯 [`fa0ab86`](https://github.com/Tencent/openclaw-weixin/commit/fa0ab862f53ae56532dbb8153f1a431de02e79dc) 将入站 sender 与扫码后保存的 `userId` 比对；不匹配时在命令处理、AI 派发和 token 写入前拒绝。它不是扫码时校验预先指定的 owner。缺失或空白 owner 时仍兼容普通消息，但不授权命令，不能称为全面 fail-closed。强制 PIN 开关也非默认开启；显式开启时才请求 `bot_spam_business_data=base64(JSON({verify_pin:1}))`，未开启时由服务端决定。
- 社区 `0.1.4` 返回扫码 `userId`，但允许缺失，且没有入站 owner 策略。非 owner 消息会被交给 Chat handler，token 在 handler 前写入 state；宿主必须在任何 Agent/工具执行前实施受信 owner 策略。其 GET 扫码请求未携带强制 PIN 字段，`need_verifycode` / `confirmed_but_need_verifycode` 在离线回放中只会等到超时。这里证明客户端差异，不证明真实服务必然拒绝旧客户端。
- 腾讯 [`808e099`](https://github.com/Tencent/openclaw-weixin/commit/808e099e47efd108c4bbc40f5fa4afc5409ca5ae) 在内存 miss 时补读同账号的 token 文件，并保留已有内存值，修复未执行 `startAccount` 的 CLI/cron 等独立进程；网关已有显式启动恢复。该补丁不延长服务端 token 有效期。
- 社区 `0.1.4` 每次发送直接读取 `StateAdapter` 的 `weixin:<accountId>:<userId>:context_token`，没有腾讯的第二层私有 Map。正常账号/用户命名空间隔离，`postMessage` 拒绝其他账号的 thread；`startTyping` 却忽略 thread 账号，仍使用当前账号 token。重启后能否恢复取决于 state 持久性；内存 state 会丢失，缺 token 时仍尝试发送。不能机械移植腾讯磁盘补读，也不能把本地 30 天 TTL 当成平台有效期。
- 本仓 Python 边界拒绝空白名单、未授权/缺失 sender、错误收件账号与缺 token；它实现的是宿主显式白名单，不是扫码 owner 自动绑定。每个 turn 保留自身 token，但无法鉴定不透明 token 的平台归属。宿主必须认证接收通道、保存账号来源，并按 `turn.account_id` 选择发送凭据；`to_dict()` 故意没有 token，不能用于凭据恢复。社区包也不检查入站 `to_user_id`：合成错路由回包可以污染当前账号 state，但没有证据真实服务会错投这种回包。

新增 [发布包行为测试](../tests/chat-sdk-weixin-security.test.ts) 与 [重启夹具](../tests/fixtures/weixin-restart.mjs)，并扩展 [Python 边界测试](../tests/test_weixin_ilink.py)。复验命令：`pnpm exec vitest run tests/chat-sdk-weixin-security.test.ts`；`PYTHONPATH=src python -m unittest discover -s tests -p test_weixin_ilink.py -v`。重启夹具使用不同 OS 进程及测试专用文件 state，不是新增生产存储。所有平台 HTTP 均被替换；行为测试通过包含“缺口复现成功”，不等于授权安全或真实微信验收通过。

## 二维码登录与鉴权

[`src/auth/login-qr.ts`](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/src/auth/login-qr.ts) 从固定 `https://ilinkai.weixin.qq.com` 发起：

- `POST /ilink/bot/get_bot_qrcode?bot_type=3`，请求体含 `local_token_list`，上游最多带最近 10 个本地 token。
- `GET /ilink/bot/get_qrcode_status?qrcode=...`，需要时加 `verify_code`。
- 客户端处理 `wait`、`scaned`、`confirmed`、`expired`、`need_verifycode`、`verify_code_blocked`、`scaned_but_redirect` 和 `binded_redirect`。
- `confirmed` 返回的账户 ID、bot token 和 API base URL 是登录结果；`binded_redirect` 是“已绑定”状态，不应伪造一份新 token。上游可按 `redirect_host` 切换轮询地址；新实现仍需明确允许的远端地址和凭证转发边界。

[`src/api/api.ts`](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/src/api/api.ts) 的鉴权 Bot POST 使用 `AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、`X-WECHAT-UIN`、应用 ID / 版本头和可选 `SKRouteTag`，并附 `base_info`。二维码获取 POST 不带 Bearer token / `base_info`；二维码状态 GET 只带应用公共头。不要把三类请求合并成无差别的鉴权包装器。

## 长轮询与消息身份

`POST /ilink/bot/getupdates` 接受不透明的 `get_updates_buf`，初次为空。响应可给出 `msgs`、新游标和 `longpolling_timeout_ms`；废弃的 `sync_buf` 不应作为当前请求字段。上游监控器只保存非空新游标，且在逐条处理消息之前保存；这是现有客户端行为，不能证明失败重启时每条消息都已持久化接收。

未来本地 host 需要先持久化入站 receipt，再以可恢复的方式推进游标，避免直接复制“先写游标、再运行消息”的顺序。超时 / 停止可使上游 API 包装器返回空结果；停止信号应结束循环。HTTP 错误与 `ret` / `errcode` 业务错误分别处理；上游对 `-14` 使用会话暂停逻辑，不应无限重试已失效凭证。

文档表格把 `message_id` 列为 number，但当前 [types.ts](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/src/api/types.ts) 已将线上的 uint64 消息 ID 保存为 string；`api.ts` 对 `message_id`、`msg_id`、`svr_id` 做无损 JSON 解析。新实现应保留字符串，不能经过 JavaScript number 再转换。

## 回复上下文与媒体

`POST /ilink/bot/sendmessage` 的 `msg` 包含目标用户、客户端生成的 `client_id`、`message_type: 2`、`message_state: 2`、`item_list`，以及适用时的 `context_token` / `run_id`。回复应携带对应入站会话的 context token；上游在缺少 token 时仍尝试发送，不能据此断言服务端接受。类型中的 `group_id` 也不能证明群聊可用。

[`src/messaging/send.ts`](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/src/messaging/send.ts) 区分本地 `client_id` 与可选服务端 `message_id`。说明文字和媒体可成为多个独立请求；单个请求成功不能证明整个复合回复都成功，也不能从 `client_id` 推断服务端具备幂等保证。

[`src/cdn/upload.ts`](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/src/cdn/upload.ts) 读取明文、计算 MD5、生成 AES-128 key，获取上传参数后通过 CDN 上传密文；大小区分明文与 AES-128-ECB / PKCS#7 密文。优先使用 `upload_full_url`，否则用 `upload_param`；公共流程设 `no_need_thumb: true`。媒体 key 有 hex 与 base64 表示，入站图片的 `aeskey` 与 `media.aes_key` 不能当成同一种编码。

本轮未实现或验证 CDN 上传、下载解密、语音格式、缩略图、群聊、主动消息或多账户微信运行。

## 后续验收

1. 以固定版本的脱敏 fixture 验证登录状态机、账户隔离、context token 关联和 uint64 消息 ID。
2. 定义入站 receipt / 不透明游标的提交顺序，验证崩溃、长轮询中止与重复批次恢复。
3. 证明发送 intent 在网络请求前持久化，超时结果保留为未知；多段文本 / 媒体分别记录回执。
4. 在授权测试账户中验收二维码绑定、文本收发、token 失效、断线重启，再独立验收媒体。没有这些记录前保持研究状态。
