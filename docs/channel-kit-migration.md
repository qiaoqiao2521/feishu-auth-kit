# Channel kit：复用优先的迁移决定

## 当前决定

先采用 `vercel/chat` 的发布版作为可嵌入 TypeScript 统一接口试点，组合现成平台
adapter；独立跨语言服务需要时再评估 Satori。旧飞书 API、CLI、Python 导入与
凭证位置保持兼容，待官方 Channel SDK 功能对照和账号验收后再逐项委托。

本草稿已撤下尚未合并的 `src/core`、自建 Telegram HTTP / 归一化 / SQLite polling
host，以及相应公开子路径。没有发布这些实验 API，也不新增另一个长期维护的通信层。
已有飞书运行时恢复为主分支实现；原授权、continuation / retry artifact 先作为兼容增量保留。

## 已经用真实依赖证明什么

| 候选 | 固定版本 | 本地证明 | 未证明 |
| --- | --- | --- | --- |
| `chat`、`@chat-adapter/telegram`、`@chat-adapter/state-memory` | `4.43.0` | 安装、类型、初始化模拟、webhook→SDK Message→handler→SDK post、按钮回调、草稿与最终发送 | 真实 bot、持久恢复与生产交付 |
| `@larksuite/vercel-chat-adapter` | `0.3.0` | 实际发布包审阅、导入、无连接构造、Chat 类型兼容 | 应用登录、WS、卡片和真实收发 |
| `@agentor/chat-qq` | `0.1.1` | 同上；构造不开 WS、不取 token | callback 缺签名修复、重连、真实应用 |
| `chat-adapter-weixin` | `0.1.4` | 同上；关闭 polling，不扫码 | 当前 iLink 协议差异、上下文 / 媒体与真实账号 |
| `@agentor/chat-wecom` | `0.1.3` | 发布包与源码审阅 | 本地运行、任一模式的真实接入 |

以上依赖仅固定在开发评估依赖与 lockfile 中；旧生产入口不导入它们。
准确的维护主体、来源提交、发布包 integrity / SHA-256 及缺口见
[复用矩阵](../protocols/reuse-matrix.md) 和 [复用锁文件](../upstream/reuse-sources.lock.json)。
包版本相同、目录收录、可构造、符合 peer range，都不等于账号接通或成熟度保证。

## 需要维护的增量

- 配置与业务授权：SDK 的收包能力不授予启动 Agent 的权限。试点为每个 bot 创建
  独立 Chat / state，必须提供精确 user / chat 授权，可限定 topic。
- 模型输入：上游 `Message.toJSON()` 含 raw、附件 URL / fetchMetadata；显式选取
  text、公开身份和引用字段，不把 SDK 对象整体序列化。企业微信部分 thread ID 含密钥，
  将来不能把所有上游 thread ID 一律当成公开标识。
- 状态命名空间：memory 无 namespace 参数且跨进程丢失。生产后端应复用 SDK 的
  state adapter，并隔离 account namespace；不把多账号放进同一个无隔离状态空间。
- 执行与投递记录：SDK 去重与锁不是业务发送账本。普通 Telegram 网络失败不会在
  adapter 内重发，但 polling 可重跑失败 handler；未知发送结果必须保留证据再决定动作。
- 平台能力与补丁：QQ callback 缺签名边界、社区微信旧登录 / uint64 类型等有证据的
  差异优先给既有 adapter 补丁与回归，不复制另一套底层。

## 试点的具体选择

[Telegram 示例](../examples/chat-sdk/telegram.mjs) 直接组合 SDK，使用 webhook-only、
强制 secret、独立 memory state、无状态 concurrent echo 与选取后的模型输入。
示例不安装 HTTP server，不切换 bot webhook，不注册 callback Agent 工具，也不启动
外部 runner。绑定真实服务、凭据注入和持久后端属于后续授权账号验收。

`concurrency: 'queue'` 会合并中间消息到 `context.skipped`，不是逐条 FIFO；默认 drop
也可能丢弃并发消息，因此试点明确选择无状态 concurrent。将来串行 Agent 需要明确
处理中间消息和取消的语义后再选上游并发策略。

核心 dedupe 在 handler 成功前写入，失败后也保留到 TTL；Telegram webhook 又有
独立的 update 去重。模拟测试对核心 TTL 的证明不能解释为 webhook 重投一定重执行。
内存 state 不提供跨进程恢复，也没有 exactly-once 保证。

## 下一轮验收停点

1. 在受控 Telegram bot 上验收现成 adapter 的 webhook、授权、topic 回复、按钮、
   最终发送失败与未知状态，接入持久 StateAdapter 和业务投递记录后再谈生产闭环。
2. 飞书优先委托官方 Channel SDK / Chat adapter；为旧返回 schema、owner policy、
   app+user token 命名空间和 continuation 保存兼容测试。
3. QQ 与微信先修已识别 adapter 缺口，做官方样本回放，再分别进行账号、重连、
   上下文与媒体验收。企业微信三种模式单独记录。
4. OpenClaw / Hermes 只做薄宿主适配，锁定版本；Satori 为独立服务对照，不把多套
   框架的路由、调度与插件系统装入本仓。

本轮未进行扫码、真实网络收发、部署或外部 Agent 执行。验证数量与 CI 结果见 PR；
旧的协议锁文件继续作为腾讯官方源码比较基线，不能替代实际发布 adapter 的版本证据。

## 本地验证记录

Node 24 / Python 3.12 下通过：`pnpm install --frozen-lockfile`、`pnpm test`
（161 项，其中 20 项针对真实 Chat SDK / 候选构造 / 薄组合语义）、
`pnpm typecheck`、`pnpm typecheck:chat`、`pnpm build`、`pnpm test:package`；
Python 203 项测试、Ruff 与 wheel / sdist 构建；Actionlint、Zizmor 离线审计及补丁检查。
`typecheck:chat` 对实际候选测试单独执行 TypeScript 检查，不将 Vitest 转译误当作类型通过。

候选依赖带入的 protobufjs postinstall 只输出依赖版本警告，不生成运行文件；本轮明确
禁用该 hook，并以完整安装和真实 adapter 导入验证发布 JS 可用。esbuild 保持原有构建许可。
