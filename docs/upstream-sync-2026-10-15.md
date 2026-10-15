# 上游同步记录：2026-10-15

本轮从 `qiaoqiao2521/feishu-auth-kit` 的 `77cf53d`（2026-09-14）开始。
它不是 GitHub fork，因此按仓库实际使用的模块和 contract 选择性吸收变化。

## 核对范围

| 来源 | 本轮核对结果 |
| --- | --- |
| [larksuite/openclaw-lark](https://github.com/larksuite/openclaw-lark) | main 为 `dde0be3`（2026-07-16）；仓库内离线 corpus 截止到 7 月 9 日，尚未覆盖后续表单修复。 |
| [openclaw/openclaw](https://github.com/openclaw/openclaw) | 读取 main 快照 `3c1f275e2a0ce8fa45499092bda3d3ffca2085b4`（2026-10-15）；重点核对 Feishu 入站解析和卡片变化。 |
| 当前仓库 issues | 核对时仅有两个 draft PR，没有独立 issue；离线 corpus 的 340 条历史追踪样本不代表当前仓库待办。 |
| 上游未合并工作 | [#639](https://github.com/larksuite/openclaw-lark/issues/639) 为开放问题；[#628](https://github.com/larksuite/openclaw-lark/pull/628) 和 SDK 兼容类 PR 尚未合并，未将其视为已发布能力。 |

## 已实现的改进

### 表单路由和操作者字段（TypeScript）

依据上游 [0e6f9ce](https://github.com/larksuite/openclaw-lark/commit/0e6f9ce3614ada1930e3fb4a150b93cc120e1c92)
及 [9920564](https://github.com/larksuite/openclaw-lark/commit/9920564d2084a6e43cd7445f4681853cbcb50266)。

`onAction()` 按 `value.action`、`value.action_id`、`action.name`、
`form_submit` 的 `form_name` 顺序解析有效路由，避免处理器注册顺序覆盖显式按钮动作。
新增可选 `actionName` 和 `formValue`，旧的预解析 `CardActionContext` 仍可使用。
SDK 事件体与 webhook `event` envelope 都能读取 `operator`、消息和会话上下文。
`userId` 与 `openId` 分别保留，避免将不同命名空间的身份混用于 owner 校验。

```typescript
const dispatcher = new InteractiveActionDispatcher()
  .onAction("question.submit", async (ctx) => {
    const choices = ctx.formValue?.choices;
    return { toast: { type: "success", content: `已收到：${choices}` } };
  });
```

### 富文本和代码块（Python / TypeScript）

依据上游 [516eec6e](https://github.com/openclaw/openclaw/commit/516eec6e5be75acb8172e8258ee3ee30c395bab6)
（2026-10-13，已合并 PR [#171222](https://github.com/openclaw/openclaw/pull/171222)）。

将 `post` 的 direct、locale 和 `post` 包装内容转换为可读 Markdown。
保留段落、代码缩进和换行，代码 fence 长于内容中最长的反引号连续段，
避免代码提前闭合。代码 fence 内不做 mention 剥离；单行文本仍保持原有空白归一化。
畸形富文本返回 `[Rich text message]`，原始事件仍可通过 `include_raw` 读取。
两端使用同一组 JSON fixtures 验证输出一致。

### 流式卡片终态交付（TypeScript）

参照上游 streaming controller 在终态等待在途 flush 的处理方式，修复本地适配时的竞态：
旧实现中 `flush()` 在已有请求时立即返回，随后 `complete()` 取消补刷，最终快照可能丢失。

现在首次 `complete()` / `abort()` / `error()` 锁定终态与快照，停止定时调度，
等待在途请求结束后发送最终快照。后续终态调用共用同一 Promise；
`waitForFlush()` 也等待最终投递。最终投递失败向调用方传播，由宿主决定重试策略。

### API 错误诊断（Python / TypeScript）

依据上游开放 issue [#639](https://github.com/larksuite/openclaw-lark/issues/639)。
HTTP 200 也可能携带 `code=21001`，需要按业务错误处理。

`FeishuApiError` 新增 `code`、`httpStatus` / `http_status`、`logId` / `log_id`、
`apiMessage` / `api_message` 和 `hint`。日志 ID 优先取 `x-tt-logid`，
其次取响应中的 `log_id`。`21001` 提示检查出口 IP 白名单和管理员访问策略，
不自动重试权限拒绝。异常不附带整段响应体，并脱敏错误消息中回显的已知凭据。
普通 `FeishuApiError("message")` 的继承关系和消息保持兼容。
`register probe --json` 失败时附加 `api_error`，文本 probe / doctor 报告也展示这些字段；
成功输出不增加诊断字段。TypeScript probe 复用同一客户端错误检查。

### 上游追踪器可靠性

工作流从当前 `GITHUB_REPOSITORY` 解析目标仓库，显式 CLI 参数仍优先。
游标模式不再受 lookback 时间窗截断；先确认旧游标，再按最早未处理提交分批推进，
避免积压大于单次上限时直接跳到 HEAD 而永久漏提交。找不到游标时明确失败，
保留状态以供排查。

## 验证与后续范围

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm typecheck
pnpm build
uv sync --extra dev --frozen
uv run pytest -q
uv run ruff check .
```

回归覆盖真实表单字段、双端富文本 fixture、在途卡片更新与终态并发、
HTTP 200 业务拒绝、非 2xx 错误和游标积压分页。
`pnpm-workspace.yaml` 仅允许 esbuild 所需的安装构建脚本。

SDK 插件注册、sender、callback server 和会话路由仍由宿主负责；
本轮没有运行需要真实飞书应用凭据的线上请求。
普通流式定时回调的错误处理及重试、OAuth HTTP 400 轮询语义、tenant token 到期刷新、
digest 更新时保留人工勾选，留待后续分别处理。
