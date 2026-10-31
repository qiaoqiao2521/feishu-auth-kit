# 渠道协议研究

核对日期：2026-10-31。源码版本和内容 SHA-256 见 [sources.lock.json](../upstream/sources.lock.json)。

本目录保留官方协议比较基线。当前选型与发布 adapter 的差异见
[复用矩阵](reuse-matrix.md)、[飞书职责对照](feishu-reuse.md) 和
[reuse-sources.lock.json](../upstream/reuse-sources.lock.json)。优先复用现成实现，
仅为缺口增加薄层或上游补丁；源码字段与构造成功不代表真实平台验收。

| 渠道 / 宿主 | 本轮证据 | 本轮实现范围 |
| --- | --- | --- |
| [Telegram](telegram.md) | 官方 Bot API 10.3 文档及方法页 | 复用 Vercel adapter 4.43.0，已完成模拟 webhook、按钮与最终投递语义测试 |
| [QQ](qq.md) | 腾讯官方插件固定提交的源码 | 对照社区 adapter 0.1.1；仅导入、构造和类型兼容通过 |
| [微信](weixin.md) | 腾讯官方插件的协议文档、API 类型、登录与发送源码 | 对照社区 adapter 0.1.4；仅导入、构造和类型兼容通过 |
| [Hermes](hermes.md) | 官方平台插件指南与注册 / adapter 源码 | 宿主接入研究；未实现 Hermes adapter |

证据分为三层：

- **文档 / 源码观察**：读到的格式、代码分支和依赖声明，固定到提交或文档内容哈希。
- **本地回放**：项目测试使用本地 fixture、模拟 HTTP 或本地状态库；只验证测试覆盖的客户端行为。
- **真实平台验收**：实际账户上的登录、接收、回复、重连和媒体操作。本轮未执行。

锁文件只记录第一层来源，不将本地测试或线上验收标记为已完成。所有 npm 包版本均来自读取到的 package.json；未下载发布包，未填写发布包校验和。源码哈希针对 GitHub 连接器返回的完整 UTF-8 内容，并核对 Git blob SHA-1；Telegram 哈希针对读取到的原始 HTTP 文档字节。
