# feishu-auth-kit → channel-kit

中文 | [English Summary](#english-summary)

本仓库正兼容演进为多平台聊天接入工具库。包名、Python 导入、飞书 CLI 和凭证位置
继续保留；当前通过子路径拆分代码边界，后续再迁移到独立 packages，避免维护两份实现。

| 入口 | 当前状态 |
| --- | --- |
| `feishu-auth-kit` / `feishu-auth-kit/feishu` | 现有飞书 API，兼容原 schema 和 runner 接口 |
| `feishu-auth-kit/core` | 平台无关的消息、身份授权、会话键、能力、投递结果和 runner 契约 |
| `feishu-auth-kit/telegram` | Bot API 文本收发、编辑、callback 应答、可选临时 draft，以及入站归一化 |
| `feishu-auth-kit/telegram-host` | 本地 SQLite 持久化回显 / runner 宿主，需要 Node.js 24+；默认拒绝未授权发送者 |
| QQ / 微信 iLink | 已锁定官方实现与协议依据；适配器和账号验收待实现 |
| OpenClaw / Hermes 薄适配 | 已研究宿主边界；本轮未提供可安装适配器 |

平台身份按 `platform + accountId` 划分；会话键还包括会话、线程和发送者，
不会自动关联同一个人的不同平台账号。原始事件、callback data 和后续平台回复 token
留在 transport 的私有 envelope 中，不进入通用消息的模型序列化。

Telegram 宿主在启动时核对 bot ID，按显式授权调用 runner，并在发送前持久化意图。
超时或进程中断后的投递保留为 `unknown`；执行中断则为 `execution_unknown`，
重启不自动重发或重新运行。临时 draft 成功不代表最终消息已投递。
本轮只有离线回放通过，没有真实账号收发验收。

### Telegram 最小回显宿主

先在本机环境或凭据管理器设置 `TELEGRAM_BOT_TOKEN`，不要写入仓库。
下列示例配置中的 bot、聊天和发送者 ID 必须替换为实际授权测试账号：

```bash
pnpm install --frozen-lockfile
pnpm build
cp examples/telegram-grants.example.json telegram-grants.local.json
# 编辑 telegram-grants.local.json，逐项填写授权身份和会话。
export TELEGRAM_BOT_ID=123456789
export TELEGRAM_GRANTS_FILE="$PWD/telegram-grants.local.json"
pnpm telegram:echo
```

默认使用 `EchoRunner`；只有本机显式设置 `CHANNEL_KIT_RUNNER=codex` 才启动现有
Codex runner。这个宿主不下载或发送媒体，不自动重试失败发送，不切换现有 webhook。
超长回复（超过 4096 字符）记录为发送前失败，尚不自动拆分。
状态默认保存在 `XDG_STATE_HOME/channel-kit/telegram-<bot-id>.sqlite`，
未配置 XDG 时使用 `~/.local/state/channel-kit/`；可通过 `TELEGRAM_STATE_FILE` 覆盖。
状态库仅保存 ID、投递状态与游标，限本机单 consumer；不是长期聊天记忆或分布式队列。
停止信号阻止后续投递，但不会强制终止已经启动的同步 runner。

详细边界见 [迁移说明](docs/channel-kit-migration.md)；
协议依据见 [protocols](protocols/README.md) 和 [来源锁定](upstream/sources.lock.json)。
上游追踪器现在从 [targets.json](upstream/targets.json) 读取五个仓库的
平台 / 契约 / 文档路径，保留六小时周期和旧飞书游标键。
它监控 GitHub 提交；Telegram 官网文档更新需重新核对并刷新来源锁定。

以下保留飞书能力与现有使用说明。

`feishu-auth-kit` 是从 `ControlMesh` 抽取出来的飞书第一方模块仓库，负责认证、
入站消息上下文、runner seam、CardKit 单卡快照，以及
card action -> continuation -> retry contract。

它最初偏 auth/onboarding，现在已经升级为更完整的 Feishu native agent kit。
当前最小验证目标很明确：在树莓派这类已有 Codex CLI 的机器上，先证明它能把飞书
消息归一化、交给本地 runner、再产出单卡运行快照，而不依赖 ControlMesh 主体。

和 ControlMesh 的关系也要说清楚：

- `feishu-auth-kit` 是从 `ControlMesh` 抽出来并持续同步的第一方模块。
- `ControlMesh` 必须包含这套能力作为内置 Feishu native runtime 组成部分。
- 其他宿主也可以单独消费它，而不必复制飞书 auth/runtime 核心逻辑。

## 为什么存在

飞书原生智能体通常会把很多东西缠在一起：

- 从零创建 app / bot
- app scope 检查与授权链接
- 用户 OAuth device flow
- owner policy
- token 持久化
- 飞书入站消息归一化
- 面向 Codex 这类本地 CLI agent 的 runner seam
- CardKit 风格的单卡进度快照
- 批量权限规划
- 缺权限卡片与 continuation
- 授权完成后的 synthetic retry artifact

这个仓库的作用，就是把这些元能力从宿主 runtime 里抽出来，做成一个独立 Python
库和 CLI，同时继续作为 ControlMesh 第一方 Feishu 模块的同步出口。宿主比如
ControlMesh、Claude Code、OpenClaw，或者自定义脚本，都可以复用它，而不是
各自重写一套飞书 auth/native runtime seam。

## 当前能力

| Area | Capability |
|---|---|
| 应用注册 | 官方 Feishu/Lark scan-to-create 新 bot / app 流程 |
| 诊断 | tenant token、app info、已授予 scopes、核心缺失权限 |
| 权限链接 | 构造 app 权限授权链接 |
| 用户认证 | OAuth device authorization 与 poll |
| Token 存储 | 基于文件的 user token 持久化，key 为 `app_id + open_id` |
| Owner policy | 严格 owner-only 或 permissive-if-unknown 校验 |
| Native inbound | 把飞书 IM event 归一化为稳定的消息上下文 envelope |
| Runner seam | `EchoRunner` 与 `CodexCliRunner`，可解析 `codex exec --json` 的最小 lifecycle/tool-step 事件 |
| Native tool selection | 原生工具列表、tool-selection JSON contract、tool-result followup prompt |
| CardKit | 生命周期、tool call/result、warning、final text 的单卡快照模型 |
| Streaming & Throttle | 吸收 `openclaw-lark` 的 `FlushController` 与 `StreamingCardController`，打字机流式输出与 300ms 防抖更新，避免触发飞书限流 |
| Reactions | IM 消息表情交互（`addReaction` / `removeReaction` / `withReactionLifecycle`），支持处理中/成功/失败状态标记 |
| Interactive Dispatcher | CardKit 按钮与回调事件分发器，支持路由、Toast 与原地更新卡片 |
| Native contract | 独立于宿主 runtime 的 card action -> continuation -> retry request/artifact contract |
| Runtime cards | 通用的缺权限卡、device-flow 卡片 payload |
| Orchestration | pending flow registry、scope merge、batch planning、synthetic retry |
| Claude surface | 面向 Claude / tool caller 的轻量 JSON wrapper |

### 上游兼容性更新（2026-10-28）

- 按 OpenClaw 10 月 25 日的修复补齐显式 assistant message 边界，工具调用前的正文可与后续累计快照一起保留。
- 定时卡片更新失败通过 `lastFlushError` 暴露，后续显式更新可恢复；终态投递失败继续向调用方传播。
- Python / TypeScript OAuth 轮询兼容 HTTP 400 的 `authorization_pending` / `slow_down`，到期后停止请求。
- 上游追踪器保留敏感模块的相关性，按破坏性变更、安全和认证行为信号分级；测试文件变更不再仅凭路径升级为 P1。

示例：在 runner 发出新的 assistant message 事件时调用边界 API；工具结果本身不需要写入正文。

```typescript
import { StreamingCardController } from "feishu-auth-kit";

const card = new StreamingCardController({ onFlush: updateExistingCard });
card.startAssistantMessage();
await card.updateAssistantMessage("我先查一下。");
// 工具完成后，runner 开始下一条 assistant message。
card.startAssistantMessage();
await card.updateAssistantMessage("查到了");
await card.updateAssistantMessage("查到了，结果如下。");
await card.complete(); // 保留两条消息的正文
```

`updateContent()` 和 `complete(finalText)` 仍覆盖整张卡片的正文。
本轮上游判断和验证记录见 [2026-10-28 同步记录](docs/upstream-sync-2026-10-28.md)。

### 上轮兼容性更新（2026-10-15）

- TypeScript 交互分发支持 `action.name`、`form_submit.form_name` 和 `formValue`，
  可读取 SDK 直接传入或 webhook envelope 中的 `operator`。
- Python / TypeScript 入站解析支持富文本 `post` 的标题、段落、链接、提及和代码块；
  多行 prompt 保留换行、代码缩进和代码内部的反引号。
- TypeScript 流式卡片在完成、取消或失败时先等待在途更新，再发送最终快照；
  首次终态生效，所有终态调用共享投递结果。
- Python / TypeScript 的 `FeishuApiError` 保留业务错误码、HTTP 状态和日志 ID，
  `21001` 增加访问控制排查提示。

上轮提交、issues、兼容性说明与验证方法见
[2026-10-15 同步记录](docs/upstream-sync-2026-10-15.md)。

## 它不是什么

- 它不是 ControlMesh 本体。
- 它不是完整的飞书 bot framework。
- 它不是飞书 sender。
- 它不是 callback server。
- 它不是 session router 或 conversation store。
- 它不会绕过 Feishu/Lark 平台策略、租户审批、应用审核或发布要求。
- 它不会假装官方飞书流程不存在。
- 它不是要把 `openclaw-lark` 整体克隆进来。

scan-to-create 走的仍然是官方 Feishu/Lark 注册面。它能把 bot/app 从零启动，
但不是任何“非官方后门”。

## 仓库边界

`feishu-auth-kit` 应该负责：

- 从零创建飞书 app 的注册与 auth primitives
- scope inspection、owner policy、token store、continuation、retry artifact
- 飞书 native 入站消息归一化
- 把归一化 turn 交给 Codex 或其他本地 agent 的 runner seam
- 原生工具选择 prompt、selection parser、tool-result followup prompt
- native card action -> continuation -> retry contract
- 保留 tool-step 结构的单卡 runtime 快照

下游宿主应该负责：

- long polling、webhook ingress server、事件订阅 plumbing
- 真正的消息/卡片发送
- callback endpoint 与卡片点击接入
- 长时 session memory 与 routing policy
- transport-specific retry 与生产部署问题

## 开发安装

### TypeScript / Node.js

```bash
pnpm install
pnpm run build
pnpm test
pnpm run typecheck
```

运行 CLI：

```bash
./dist/cli.js setup
# 或通过 pnpm link -g / npm link 在系统 PATH 中提供 feishu-auth-kit
```

### Python

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```

运行 CLI：

```bash
uv run feishu-auth-kit setup
```

## 从零创建应用

这是 OpenClaw 风格的 first mile：用户扫码官方 Feishu/Lark QR 流程，kit 返回
宿主 runtime 可以保存的 app credentials。

```bash
feishu-auth-kit register scan-create --no-poll --json
```

输出包含：

- `qr_url`
- `device_code`
- `user_code`
- `interval`
- `expires_in`

After the user scans:

```bash
feishu-auth-kit register poll \
  --device-code dev_xxx \
  --interval 5 \
  --expires-in 600 \
  --poll-timeout 120 \
  --json
```

轮询成功时返回：

- `app_id`
- `app_secret`
- `domain`
- `open_id` when available

一条命令跑完的形式：

```bash
feishu-auth-kit register scan-create \
  --poll-timeout 180 \
  --write-env-file ./.feishu-auth.env \
  --json
```

`--write-env-file` writes local env-style values such as `FEISHU_APP_ID` and
`FEISHU_APP_SECRET`. It does not mutate your shell globally.

## 已有应用诊断

如果宿主已经有 `app_id` 和 `app_secret`，kit 可以围绕这个 app 做校验和规划。

```bash
feishu-auth-kit doctor \
  --app-id cli_xxx \
  --app-secret yyy \
  --brand feishu
```

也支持环境变量：

```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=yyy
export FEISHU_BRAND=feishu
```

For global Lark tenants, use `--brand lark`.

## Token 持久化

用户 token 通过文件存储，key 为 `app_id + user_open_id`。

默认路径解析：

- `FEISHU_AUTH_KIT_TOKEN_STORE`
- `XDG_DATA_HOME/feishu-auth-kit/user_tokens.json`
- `~/.local/share/feishu-auth-kit/user_tokens.json`

示例：

```bash
feishu-auth-kit tokens save \
  --app-id cli_xxx \
  --user-open-id ou_user \
  --access-token token \
  --refresh-token refresh \
  --scope offline_access

feishu-auth-kit tokens status \
  --app-id cli_xxx \
  --user-open-id ou_user \
  --json

feishu-auth-kit tokens remove \
  --app-id cli_xxx \
  --user-open-id ou_user
```

也可以在登录后直接保存：

```bash
feishu-auth-kit login \
  --app-id cli_xxx \
  --app-secret yyy \
  --scope im:message:readonly \
  --save-user-open-id ou_user
```

## Owner Policy

Owner 检查复用常规的 app-info 查询路径。

```bash
feishu-auth-kit owner-check \
  --app-id cli_xxx \
  --app-secret yyy \
  --current-user-open-id ou_user \
  --mode strict_owner \
  --json
```

模式：

- `strict_owner`: the current user must match the effective app owner.
- `permissive_if_unknown`: continue only when owner metadata is unavailable;
  known mismatches still fail.

## Runtime 卡片与 Continuation

这个 kit 只构造与 messenger 无关的 JSON payload，不自己发卡片。

```bash
feishu-auth-kit runtime permission-card \
  --app-id cli_xxx \
  --scope offline_access \
  --permission-url "https://open.feishu.cn/app/cli_xxx/auth?q=offline_access" \
  --operation-id op_123
```

Then, after a user clicks or says they completed the step:

```bash
feishu-auth-kit runtime continue \
  --operation-id op_123 \
  --action permissions_granted_continue \
  --actor-open-id ou_user
```

Default continuation store:

- `FEISHU_AUTH_KIT_CONTINUATION_STORE`
- `XDG_STATE_HOME/feishu-auth-kit/continuations.json`
- `~/.local/state/feishu-auth-kit/continuations.json`

## 最小 Native Runtime 与 Codex 验证

第一阶段的 native-runtime 目标刻意收得很窄：先证明这个仓库在接入
ControlMesh 之前，就已经能独立作为 Feishu native agent substrate 存在。

1. 把飞书入站消息归一化成稳定 context envelope。
2. 把这个 envelope 交给 runner seam。
3. 产出包含最终文本和可选 tool steps 的单卡快照。

Normalize an inbound Feishu event:

```bash
feishu-auth-kit agent parse-inbound --event-file ./examples/feishu-event.json
```

不用外部 agent 的最小本地 demo：

```bash
feishu-auth-kit agent run \
  --event-file ./examples/feishu-event.json \
  --runner echo \
  --echo-prefix "Codex stub"
```

机器上已安装 Codex CLI 时，可跑最小 Codex-facing demo：

```bash
feishu-auth-kit agent run \
  --event-file ./examples/feishu-event.json \
  --runner codex \
  --model gpt-5.4 \
  --codex-cd .
```

输出是一个 JSON，包含：

- 归一化后的 Feishu message context
- runner request payload
- runner result
- 一个 `feishu-auth-kit.cardkit.single_card.v1` 快照，可供未来的 Feishu card sender 直接消费

如果你想拿到 JSONL 事件流，而不是单个聚合 payload：

```bash
feishu-auth-kit agent run \
  --event-file ./examples/feishu-event.json \
  --runner codex \
  --codex-cd . \
  --emit-events
```

当前最小 Codex 事件语义：

- 生命周期：`start`、`running`、`completed`、`error`
- tool-step seam：`tool_call`、`tool_result`
- 用户可见输出：`assistant_message`
- 非致命日志：`stderr_warning`

这套语义是故意保持最小的。它还没有覆盖 Codex 的所有内部 item type、token 级
stream，也还没有建模多消息多轮 transcript。

## Native Card Action Contract

`feishu-auth-kit` 现在定义了一套独立于 ControlMesh 的 native contract：

- `NativeCardAction`
- `NativeContinuationRecord`
- `NativeRetryRequest`

目标流程是：

1. auth/orchestration 先创建 continuation
2. `agent bind-continuation` 把它升级成 native retry contract
3. 未来的 Feishu sender/callback runtime 把按钮点击转成 `NativeCardAction`
4. `agent action-to-retry` 把这个 action 还原成 retry request 和 synthetic retry artifact

示例：

```bash
feishu-auth-kit agent bind-continuation \
  --operation-id op_123 \
  --text "Please continue the previous operation."

feishu-auth-kit agent action-to-retry \
  --operation-id op_123 \
  --action permissions_granted_continue \
  --actor-open-id ou_user
```

第二条命令会输出：

- 归一化后的 card action
- 已确认的 native continuation record
- 一个 `feishu-auth-kit.native-retry-request.v1`
- 一个 `feishu-auth-kit.synthetic-retry.v1`

这个仓库仍然不包含真正的 Feishu sender 或 callback server。像 ControlMesh 这类
宿主 runtime 可以在后面消费这些 contract。

## Auth Orchestration

orchestration 层负责把缺权限和认证失败转换成可复用的宿主动作。

规划一组 scope 请求：

```bash
feishu-auth-kit orchestration plan \
  --requested-scope offline_access,im:message:readonly \
  --app-scope offline_access,im:message:readonly \
  --user-scope offline_access
```

路由一个缺权限事件：

```bash
feishu-auth-kit orchestration route \
  --app-id cli_xxx \
  --error-kind app_scope_missing \
  --required-scope offline_access \
  --user-open-id ou_user \
  --flow-key flow-1 \
  --permission-url "https://open.feishu.cn/app/cli_xxx/auth?q=offline_access"
```

认证完成后构造 retry artifact：

```bash
feishu-auth-kit orchestration retry \
  --operation-id op_123 \
  --text "Please continue the previous operation."
```

校验 device-flow identity：

```bash
feishu-auth-kit orchestration verify-identity \
  --brand feishu \
  --access-token token \
  --expected-open-id ou_user
```

## 最小 Claude Surface

Claude 或其他 tool caller 可以向这个 kit 请求结构化 JSON，再自行渲染或转发。

```bash
feishu-auth-kit claude permission-card \
  --app-id cli_xxx \
  --scope offline_access \
  --permission-url "https://open.feishu.cn/app/cli_xxx/auth?q=offline_access" \
  --operation-id op_123
```

For zero-start onboarding:

```bash
feishu-auth-kit register scan-create --no-poll --json
feishu-auth-kit register poll --device-code dev_xxx --json
```

## Python API

```python
from feishu_auth_kit import (
    AppRegistrationClient,
    ContinuationState,
    DeviceFlowClient,
    FeishuAuthClient,
    FileContinuationStore,
    FileTokenStore,
    build_device_flow_card,
    check_owner_policy,
)
from feishu_auth_kit.scopes import batch_scopes, filter_sensitive_scopes

client = FeishuAuthClient("cli_xxx", "secret", brand="feishu")
app_info = client.get_app_info()
owner = check_owner_policy(app_info, current_user_open_id="ou_owner")
user_scopes = client.get_granted_scopes(token_type="user")
safe_batches = batch_scopes(filter_sensitive_scopes(user_scopes), batch_size=100)

registration = AppRegistrationClient()
begin = registration.begin()
poll = registration.poll(
    begin.device_code,
    interval=begin.interval,
    expires_in=begin.expires_in,
)

device = DeviceFlowClient("cli_xxx", "secret")
authorization = device.request_authorization(safe_batches[0])
card = build_device_flow_card(
    app_id=client.app_id,
    operation_id="op_123",
    authorization=authorization,
)

FileContinuationStore().save(
    ContinuationState(
        operation_id="op_123",
        app_id=client.app_id,
        kind="device_flow_authorization",
        status="waiting",
        payload=card.to_dict()["fields"],
    )
)
FileTokenStore().status(client.app_id, "ou_owner")
```

## 宿主集成契约

宿主 runtime 应该负责：

- message send 或 card render
- webhook 或 long-poll ingress
- callback ingress
- chat / session binding
- 把 retry 注回自己的 orchestrator

`feishu-auth-kit` 应该负责：

- app registration
- app/scope inspection
- owner policy
- user OAuth primitives
- token 与 continuation state
- 入站消息上下文归一化
- 本地 runner seams
- native tool-selection / tool-result prompt contract
- native card action -> continuation -> retry contract
- 单卡 step model
- 权限与认证规划
- synthetic retry artifact

这样的拆分可以保证这个仓库可复用，也能让下游 runtime 对自己的职责边界保持清醒。

## 验证

```bash
uv run ruff check .
uv run pytest -q
```

## English Summary

The repository is evolving into a channel kit while retaining the existing
Feishu package, Python API and CLI. New `/core`, `/telegram` and optional
`/telegram-host` exports provide shared contracts and a locally replay-tested
Telegram Bot API path. QQ, Weixin iLink and host plugin adapters remain research
and follow-up work. No live platform acceptance is claimed.

`feishu-auth-kit` is the extracted first-party Feishu native agent module from
`ControlMesh`. It owns auth/onboarding primitives, inbound message
normalization, runner seams, single-card snapshots, and the native card action
-> continuation -> retry contract.

`ControlMesh` should include it as a bundled Feishu native runtime component,
while other hosts may also consume the same module standalone.
