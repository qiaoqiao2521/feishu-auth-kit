# 飞书优先复用审计

核对日期：2026-10-31。本页区分现成能力、旧接口兼容层与尚未完成的接入；没有安装依赖、替换传输或执行真实飞书操作。

## 选择现有层级

多平台统一接口优先采用 Vercel **Chat SDK** 的 `Adapter`、`Message`、`Thread` 和 `StateAdapter`。飞书优先使用官方 `@larksuite/vercel-chat-adapter`；只需要飞书专属能力时，直接使用官方 **Lark Channel SDK**，不在 kit 里另写一套连接、收发、富文本解析或 CardKit 流控。

| 现有组件 | 本次固定源码观察 | 适合委托的职责 |
| --- | --- | --- |
| [`vercel/chat` 的公共接口][chat-types] | 统一 adapter、消息/线程、订阅与 state/lock 接口；仓库同时维护 Telegram adapter | 多平台消息接口及宿主状态接入。runner 不需要复制 Chat 的平台路由和状态机。 |
| [`@larksuite/vercel-chat-adapter` 0.3.0 发布包][adapter-tarball] | 已检查发布包 `package.json`、`README.md` 和 `dist/index.js`；直接依赖 `@larksuite/channel` 0.7.1，peer 为 `chat ^4.26.0` | 飞书与统一 Chat 接口之间的现成桥，不需要另造 Chat adapter。 |
| [`@larksuite/channel` 0.7.1][node-manifest] | `createLarkChannel`；WS/webhook、归一化、策略与去重、发送/编辑/流式、媒体、卡片回调和 reactions | TypeScript 飞书专属通道。公开入口还导出 `registerApp`，不只是底层 REST SDK。 |
| [`lark-channel-sdk` 1.4.0][python-version] | `FeishuChannel` 高层收发与通道生命周期；公开 auth/store 类型与 `require_user_auth` | Python 飞书通道，以及用户 token 的 device flow、缓存、刷新和授权提示。 |
| [`@larksuiteoapi/node-sdk` 1.76.0][node-sdk-manifest] | 已有 `channel/` 高层实现和 `scene/registration/` 扫码注册 | 官方底座及兼容出口；不因为它也提供低层 OpenAPI 就忽略已有高层能力。 |

Channel/node-sdk 版本号来自各固定提交的源码声明；Chat adapter 版本来自实际下载的发布包。发布包已核对 registry SHA-512，SHA-256 为 `ffee7f9197b065bfba9c784991e025e20ccf387ce40bb8c33bb79128dbb61dfe`，记录见 [sources.lock.json](../upstream/sources.lock.json)。下载并读取发布代码不代表安装、运行或完成线上验收。Node Channel SDK 的 [`src/registration.ts`][node-registration] 直接重导出 node-sdk `registerApp`，两者不是两个独立的扫码协议。

## 与旧 kit 的职责对应

| 旧 kit 能力 | 现成上游覆盖 | 应保留的兼容边界 |
| --- | --- | --- |
| `AppRegistrationClient`、CLI `register scan-create` | Node Channel 的 `registerApp` 已完成官方 QR begin/poll；node-sdk 注册选项还支持 `addons`、`appId`、`createOnly`。 | 旧 CLI 参数、JSON 字段、`client_id` → `app_id` / `client_secret` → `app_secret` 映射与本地凭据保存约定。`--no-poll` / 单独 `register poll` 的跨进程恢复接口不能未经比对直接删掉。 |
| `DeviceFlowClient` | Python SDK 已有 `DeviceFlowClient.start/poll/refresh`；`FeishuChannel.require_user_auth` 封装 cache → refresh → prompt → poll。 | 双端既有返回值、scope 策略和 CLI 行为。Python 侧应适配现有引擎，不能因旧 kit 有同名类就再维护一份 HTTP 轮询。此次检查的 Node Channel 公开入口没有对应的完整用户 UAT device-flow API，不能宣称两端已可一比一替换。 |
| `FileTokenStore` | Python 提供 `TokenStore` protocol、内存与文件实现。 | 旧 `app_id + user_open_id` 键空间和文件格式。上游 store 的方法以 `user_id` 为键，应按 app 隔离实例或包装 key，不能让多个 app 共用无命名空间的用户键；格式迁移应显式完成。 |
| 入站消息归一化、流式卡片、表情、按钮分发 | Node/Python Channel 已有相应高层模块；官方 Chat adapter 也暴露飞书 rich content。 | 旧 `feishu-auth-kit.*.v1` schema 和兼容 fixture。只对这些边界做字段映射，不把完整 raw event、callback value、token 或资源下载凭据放进 runner prompt。 |
| `owner-policy` 与宿主授权 | SDK 提供 mention、DM/group allowlist 等入口策略，Chat 负责消息路由。 | kit 已有 app/account、owner 与操作发起人约束。SDK `senderId` 可能回落到其他 provider ID，不能自动把 `user_id` 当成旧 `open_id`；按钮操作者仍需单独核验。 |
| `scopes`、probe/doctor、permission card | 注册 `addons` 能提出 scopes/events/callbacks；SDK 提供原始 OpenAPI 逃生口及 auth 参数。 | 现有“应用缺权 / 用户未授权 / 用户 scope 不足”诊断和对外错误信息；`addons` 不等于平台已经授予权限，SDK 连通也不等于用户有权执行某工具。 |
| `PendingAuthFlow`、`NativeContinuationRecord`、`SyntheticRetryArtifact` | SDK 能交付 card action 事件/响应、取得 token；这不自动构成 kit 的授权后继续执行协议。 | `operation_id`、原始请求关联、required scopes、首个决定、持久化 continuation 和显式 retry artifact。它们是宿主兼容协议，应在薄桥接层保留，不能用重新发送普通消息替代。 |

Node 扫码注册与用户 UAT 授权是两条不同流程：前者创建/绑定应用凭据，后者取得用户授权。Python 高层 SDK 的 UAT 覆盖已有[公开入口][python-public]和[实现][python-uat]依据；本次未在其公开入口发现与 Node `registerApp` 对等的扫码创建接口，因此不把二者混为“SDK 都已经替换完成”。

## 官方 Chat adapter 的版本边界

[`vercel/chat` 的 vendor-official 目录][lark-catalog]列出了 `@larksuite/vercel-chat-adapter`。本次还读取了 **0.3.0 实际发布代码**：`dist/index.js` 从 `@larksuite/channel` 导入 `createLarkChannel`、`normalize` 和 `registerApp`；`initialize()` 使用该 channel 的事件与连接，`disconnect()` 交回其断连方法。

- 代码固定 `transport: 'websocket'`，`handleWebhook()` 返回 501；不能因为底层 Channel 支持 webhook，就宣称这个版本的 Chat adapter 也支持。
- 代码配置 `staleMessageWindowMs: Number.MAX_SAFE_INTEGER`、关闭 `chatQueue`、批处理 delay 为零，并调用 Chat 的 `processMessage`。发布包说明由 Chat + StateAdapter 承担相应安全/状态职责。选用该路径时应明确由哪一层消费、去重和加锁，避免同时启动 Chat 和另一套 kit 接收器。
- thread ID 为 `lark:{chatId}:{rootId}`，由 `deriveRootId` 映射消息根。`root_id` 是消息根；飞书 `omt_*` 的原生 `thread_id` 是话题容器，不能直接当 `replyTo` 消息 ID。
- `isDM()` 对 `ou_*` 占位会话直接判定，普通 `oc_*` 依赖当前进程见过的 p2p 事件缓存；不能只看 chat ID 前缀或把缓存当持久授权。
- 发布包的名称固定为 `lark`，单 app、多租户和富交互限制仍需结合实际部署方式验证；目录登记本身不是功能验收。

node-sdk 固定提交中的[旧适配器 README][adapter-readme]仍写底座为 `@larksuiteoapi/node-sdk`；0.3.0 发布包实际已经直接依赖独立 `@larksuite/channel`。两层证据均保留，但以具体发布包的依赖与代码判断该版本行为。发布元数据所指 `larksuite/vercel-chat-adapter` GitHub 仓库此次访问返回 404，因此不把它配置成会持续失败的 commit-watch target；发布包和官方目录证据另行保留。

## 本轮落实与后续接入

本轮将官方 Channel Node/Python、node-sdk 的高层/注册区域和 `vercel/chat` 的统一接口加入上游追踪；原有 `openclaw-lark`、`openclaw-core`、QQ、微信和 Hermes 的游标键保留。已核实的社区实现另跟踪 `DemoMacro/agentor` 的 QQ / 企业微信两个 package，以及规范仓库名 `wong2/chat-adapter-weixin`；企业微信 `wecom` 与个人微信 `weixin` 分开标记。追踪限于相关源码、包清单和明确列出的协议/接口文档，避免把整个 agent framework 的变动都当作平台协议变化。

后续接入应先锁定实际发布版本，使用现成 adapter 建立最小收发，再用旧 CLI/schema、scope/token namespace 和 continuation fixture 验证薄兼容桥。只有上游确实没有提供、且旧接口仍需保留的部分继续由 kit 实现。本页没有将这些待接入项标为已完成。

[chat-types]: https://github.com/vercel/chat/blob/6ce9852ddb0ebd4e122435fafd7dfda8431243ff/packages/chat/src/types.ts
[node-manifest]: https://github.com/larksuite/channel-sdk-node/blob/ecdec28389a96b2cf668766b6d110fb24c8610f5/package.json
[node-registration]: https://github.com/larksuite/channel-sdk-node/blob/ecdec28389a96b2cf668766b6d110fb24c8610f5/src/registration.ts
[node-sdk-manifest]: https://github.com/larksuite/node-sdk/blob/b188be60134d409b61259546bcf69c731d2b0b7c/package.json
[python-version]: https://github.com/larksuite/channel-sdk-python/blob/52ba7968a4a6898f653eb0690ce98b69ddb1354d/lark_channel/core/const.py
[python-public]: https://github.com/larksuite/channel-sdk-python/blob/52ba7968a4a6898f653eb0690ce98b69ddb1354d/lark_channel/channel/__init__.py
[python-uat]: https://github.com/larksuite/channel-sdk-python/blob/52ba7968a4a6898f653eb0690ce98b69ddb1354d/lark_channel/channel/auth/uat_runner.py
[lark-catalog]: https://github.com/vercel/chat/blob/6ce9852ddb0ebd4e122435fafd7dfda8431243ff/apps/docs/content/adapters/vendor-official/lark.mdx
[adapter-readme]: https://github.com/larksuite/node-sdk/blob/b188be60134d409b61259546bcf69c731d2b0b7c/docs/vercel-chat-adapter/README.md
[adapter-tarball]: https://registry.npmjs.org/@larksuite/vercel-chat-adapter/-/vercel-chat-adapter-0.3.0.tgz
