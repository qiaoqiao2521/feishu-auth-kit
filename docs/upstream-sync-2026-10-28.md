# 上游同步记录：2026-10-28

本轮续接草稿 [PR #3](https://github.com/qiaoqiao2521/feishu-auth-kit/pull/3) 的
`be22b58`，并整合主分支 `a3669c8` 的仓库迁移、依赖维护及工作流检查。

## 上游核对

| 来源 | 核对结果与处理 |
| --- | --- |
| [larksuite/openclaw-lark](https://github.com/larksuite/openclaw-lark) | main 仍为 `dde0be3`（2026-07-16），上轮已覆盖表单、operator 和富文本兼容性，本轮没有新增提交可吸收。 |
| [OpenClaw v2026.10.8](https://github.com/openclaw/openclaw/releases/tag/v2026.10.8) | 2026-10-27 发布；核对到 Feishu 路径的 `591b7795fe8b64aba2b84ee5764534da545b3869`（2026-10-28）。按本仓库模块职责选择性同步。 |
| [860a4de](https://github.com/openclaw/openclaw/commit/860a4ded04d74cb945c5bc277a0ccbe077c97102) | 2026-10-25 修复工具调用前后流式正文丢失。移植显式消息边界和消息内累计快照语义。 |
| [64b2e81](https://github.com/openclaw/openclaw/commit/64b2e8103ef3c384ca8e9ee6975199a9c0266192) | 修复 schema 2.0 卡片的旧 action 容器。本仓库现有 payload 使用旧版 schema，未直接套用只适合 2.0 的容器替换。 |
| [e68b0bd](https://github.com/openclaw/openclaw/commit/e68b0bde68a866cac16dc32b10611f8c205e9600) | 活跃 turn 期间接纳控制命令，属于宿主 ingress / queue 职责，本仓库没有该调度层。 |
| [5b4f3a4](https://github.com/openclaw/openclaw/commit/5b4f3a4c00e1311887a916579b22f565a9743b8e) | 追踪 issue #12 标为 P1，实际相关修改为返回类型与私有类型整理，没有新的认证协议要求。用于校准追踪分级。 |

## 本轮实现

### 流式正文边界

新增 `startAssistantMessage()` 冻结之前的正文；`updateAssistantMessage(text)`
只替换当前消息，支持较短修订、清空和重复文本。非空消息之间加入两个换行。
同一消息的 `appendToken()` 仍接收增量。`updateContent()` 重置边界并覆盖整段正文；
`complete(finalText)` 也继续作为整段最终覆盖，`complete()` 则保留累计正文。
宿主需要把 runner 的 assistant message 开始事件映射到边界调用，
不按文本长短、重叠内容或工具事件猜测消息边界。

### 定时 flush 失败

统一捕获三种定时触发路径的拒绝，避免未处理的 Promise 拒绝。
`FlushController` 和 `StreamingCardController` 均提供 `lastFlushError`；
失败时取消待刷新工作，保留正文，成功刷新后清除错误。
新的显式更新可以恢复，控制器不自行重发失败的请求。
直接 `await flush()` 的错误仍然拒绝；`waitForFlush()` 仅等待在途请求结束，
不等待尚未触发的定时器，也不将旧失败重新抛出。
流式终态继续等待旧请求结束后发送最终快照，最终投递失败向调用者传播。

### 双端 OAuth 轮询

修复上轮记录的 HTTP 400 轮询兼容性缺口。
只有成功 HTTP 响应或 HTTP 400 中的 `authorization_pending` / `slow_down`
允许继续轮询，`slow_down` 持续增加后续间隔。
其他失败状态不会被 pending 内容掩盖，也不接受非成功响应中的 token。
每次等待后重新检查到期时间；Python 使用单调时钟，避免系统时间调整影响期限。
HTTP 错误不回显原始响应体，结构化错误描述对已知凭据进行脱敏。

### 上游追踪分级

敏感文件路径继续提供相关性，但不再自动意味着 P1。
P1 需要相关生产文件与明确破坏性、安全或认证行为信号；
普通生产修改保留 P2，相关测试 / fixture 修改为 P3。
解析完整提交消息中的 `BREAKING CHANGE` footer，避免仅看标题漏掉破坏性声明。

仓库内 340 条历史样本的分类分布由 P1/P2/P3/过滤 = 40/71/19/210
变为 1/94/35/210；这是规则输出变化，不代表人工标注的准确率。
已发布 digest 不在本轮自动重写。

## 验证与边界

回归覆盖工具前正文保留、较短及空快照、终态保护、各定时 flush 失败、
显式恢复、HTTP 400 pending / slow_down、错误状态和到期边界，
以及真实追踪误报形态与正向 critical 示例。

验证通过：TypeScript 141 项测试、类型检查和构建；Python 146 项测试、
Ruff 和 sdist / wheel 打包。Actionlint 1.7.12、Zizmor 离线审计
（medium severity / confidence）及补丁格式检查也通过。
本轮没有使用真实飞书应用凭据做线上验证。

后续独立范围：tenant token 的到期刷新，以及 digest 更新时保留人工勾选。
