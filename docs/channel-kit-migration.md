# Channel kit 迁移：第一阶段范围

本轮将飞书 kit 中可复用的消息与运行时契约提取为渠道无关的 core，并以 Telegram 文本轮询作为首个新渠道试点。已有飞书 root API 和 Python 包保持兼容；暂不拆成重复实现的多个发布包。

## 模块边界

| 入口 / 模块 | 第一阶段职责 |
| --- | --- |
| `feishu-auth-kit` | 现有飞书 API 与兼容入口 |
| `feishu-auth-kit/core` | 标准化 ChannelMessage / ChannelEnvelope、AuthorizationGrant、能力与运行结果、runner 契约 |
| `feishu-auth-kit/telegram` | Telegram 文本收发 / 编辑、callback 应答、显式 draft 预览和入站归一化 |
| `feishu-auth-kit/telegram-host` | 本机单 consumer 的持久化 polling host |
| `examples/telegram-echo.mjs` | 默认 EchoRunner 的离线友好试点；Codex runner 需显式启用 |
| `protocols/` 与 `upstream/sources.lock.json` | 平台协议研究、固定来源与证据边界 |

Telegram host 使用 Node.js 24 的内置 `node:sqlite`；飞书 root 不导入该 host。下游只用飞书能力时，不需要为此启用 SQLite host。包目录拆分留给后续迁移，当前 subpath exports 是代码边界。

## 第一阶段能力与限制

本轮 core 不管理平台凭证、网络连接、agent 模型选择或工具权限。平台 transport 负责取得可验证的入站身份，宿主提供按平台、账户、bot、聊天、发送者及适用 thread 限定的授权。标准化事件本身不能授予权限。

Telegram host 的目标是：默认拒绝未授权消息，核对 `getMe` 的 bot 身份，先持久化入站 receipt / 发送 intent，再推进处理状态与游标。持久化 store 只支持本机单 consumer；它不是多机分布式队列。

网络请求已经开始但未取得可靠结果时，状态必须保留为未知，重启不自动重发。用户或运维需要依据平台回执核对结果；不会把超时解释为“肯定没发出去”。这提供可恢复记录，不构成 exactly-once 平台交付保证。

| 渠道 / 集成 | 本轮交付 | 尚未完成 |
| --- | --- | --- |
| 飞书 | 现有 API 通过兼容入口继续使用 | 新 host 与飞书真实收发验收 |
| Telegram | 文本收发 / 编辑、callback 应答、可选 draft 预览、归一化、持久化 polling host、本地回放 | 真实 bot 登录 / 收发验收，媒体下载 / 发送与 webhook host |
| QQ | 官方来源锁定与协议研究 | adapter、凭证管理、连接、回复和媒体验收 |
| 微信 | 官方来源锁定与协议研究 | adapter、扫码、游标、context token、加密媒体验收 |
| OpenClaw / Hermes | 宿主契约研究与薄 adapter 方向 | 可安装 adapter 与宿主内端到端验收 |

本轮不将 media、card、edit、typing、reaction 等平台专有能力强行映射成所有渠道共有能力；没有实现的操作应按 capability 拒绝。Telegram 归一化器只保留 file_id 附件引用，不执行下载；draft 只作预览，最终文本仍需独立发送并保存结果。

## 迁移步骤

1. 现有飞书调用方继续使用 root 入口；需要跨渠道 runner 时改为依赖 core 的标准化 envelope 与运行结果。
2. Telegram 调用方使用独立 client / host subpath，显式配置目标账户、bot 身份、授权范围和持久化目录。试点先使用 EchoRunner。
3. 根据 runner 显式配置模型或外部执行器；平台消息或原始 payload 不能决定启用 Codex、修改授权或切换账户。
4. 扩展新渠道前先锁定官方资料，补齐协议 fixture 与能力矩阵，再实现 transport / host。QQ 和微信目前停留在来源研究阶段。
5. 发布包物理拆分时保留 root 和 subpath 的兼容测试，避免出现两套 core 实现或仅用于占位的 adapter。

## 验收证据

本 PR 的验收上限是离线：标准化 fixture、授权 / 账户 / 会话隔离、模拟 HTTP、本地 SQLite 崩溃恢复与旧 API 回归。准确命令与通过数量以本 PR 验证记录为准；文档不预先宣称未运行的测试通过。

本轮已通过 TypeScript 240 项测试、Python 185 项测试、类型检查、双端构建与
Python Ruff 检查；新增打包入口 smoke 检查确认五个公开入口可加载，
包括可选的 `node:sqlite` host。Actionlint 与 Zizmor 离线审计通过。
新增 CI 对 Node.js 24 和 Python 3.10 / 3.12 运行库回归；CI 结果以对应 PR 为准。

[协议目录](../protocols/README.md) 区分文档观察、本地回放和真实平台验收。[锁文件](../upstream/sources.lock.json) 固定本轮实际读取的 20 个 GitHub 源文件和 12 个 Telegram 官方文档快照。未下载 npm 发布包，`release_artifacts` 为空；声明的包版本不是已验证安装包。

本轮没有可用于真实平台测试的凭证，没有执行扫码、发消息、部署 webhook 或运行外部 agent 的线上验收。下一阶段先在授权测试账户中完成一个受控文本往返，再验证重启 / 拒绝 / 未知发送结果；媒体、QQ、微信和宿主薄 adapter 分别设置验收记录。
