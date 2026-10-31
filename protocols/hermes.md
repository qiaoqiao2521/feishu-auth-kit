# Hermes：平台插件接入边界

来源：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/tree/f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f)，提交 `f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f`，提交日期 / 核对日期 2026-10-31。文件哈希见 [锁文件](../upstream/sources.lock.json)。这是宿主 adapter 研究，本 kit 尚未实现 Hermes 插件。

官方 [Adding a Platform Adapter](https://github.com/NousResearch/hermes-agent/blob/f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f/website/docs/developer-guide/adding-platform-adapters.md) 推荐第三方使用 plugin path：插件目录内放 `plugin.yaml` 和 Python adapter，在 `register(ctx)` 中调用 `ctx.register_platform(...)`。不需要为第三方平台修改 Hermes 核心的多处枚举。

## 注册与生命周期

[`gateway/platform_registry.py`](https://github.com/NousResearch/hermes-agent/blob/f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f/gateway/platform_registry.py) 用 `PlatformEntry` 保存名称、adapter factory、配置检查、用户授权环境变量和能力元数据。`check_fn` 是无副作用的依赖可用性检查；如需要安装依赖，`ensure_deps_fn` 才属于连接前主动阶段。状态查询不能顺带安装 SDK。

[`gateway/run_adapters.py`](https://github.com/NousResearch/hermes-agent/blob/f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f/gateway/run_adapters.py) 先查询插件注册表，再回退 built-in 路径；已注册平台创建失败时不会再悄悄回退 built-in。

[`BasePlatformAdapter`](https://github.com/NousResearch/hermes-agent/blob/f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f/gateway/platforms/base.py) 的核心接口是异步 `connect(*, is_reconnect=False)`、`disconnect()`、`send(chat_id, content, reply_to=None, metadata=None)`；入站通过 `handle_message(event)` 进入 gateway。`SendResult` 区分成功、消息 ID、错误、可重试信息及分段 ID。重连语义需要保留平台服务器上的待处理更新。

## 与 channel kit 的边界

未来薄 adapter 应映射已经验证的 transport、标准化消息和回复上下文；Hermes 仍负责 agent 生命周期、宿主授权、session 路由和 UI。不应在 kit 中复制 Hermes 的 gateway、工具授权或 agent loop。

TypeScript kit 不能直接当作 Python 的 `BasePlatformAdapter` 注册。需先明确 Python 兼容层或独立进程的调用、取消、错误和消息序列化边界，再编写 Hermes adapter；当前没有承诺任何一种互操作实现。

普通聊天平台不能使用 `trusted_inbound` 跳过用户 allowlist / pairing。凭证持有者与消息发送者是不同主体；平台认证成功也不等于允许任意发送者执行 agent。

## 后续验收

1. 固定部署中的 Hermes 版本，验证 plugin discovery、`register_platform` 元数据和 factory 调用。
2. 用本地假 transport 证明事件、account / chat / sender / thread 映射与取消 / 停止行为。
3. 验证宿主授权、跨账户拒绝、重连去重、分段发送及未知投递结果不会变成自动重发。
4. 在真实 Hermes gateway 和授权测试 bot 上执行一条完整文本往返，再分别增加媒体与平台专有 UI。当前只完成文档和源码观察。
