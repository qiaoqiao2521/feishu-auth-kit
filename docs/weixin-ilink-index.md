# 微信 iLink 接入索引（实验性，2026-11-17）

目标：在本 kit 增加独立微信协议边界，**不把微信伪装为飞书事件、不恢复已停用的 OpenClaw 服务、不改飞书生产入口**。

## 证据锚点

本次直接阅读腾讯仓库 `Tencent/openclaw-weixin` 的提交
`0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e`。其 package.json 为
`3.0.0-beta-0`，属于预发布，不等同 npm latest，也不是腾讯保证任意第三方桥可用的承诺。

- [官方仓库 README](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/README.md)：产品边界与运行条件。
- [协议类型](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/src/api/types.ts)：消息、游标、媒体、回复类型。
- [HTTP 请求实现](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/src/api/api.ts)：headers、API 路径、超时。
- [绑定/扫码实现](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/src/auth/login-qr.ts)：版本差异、扫码状态和重定向不能照抄旧教程。
- [文本发送构造](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/src/messaging/send.ts)：用户、client_id、context_token 绑定。
- [会话过期保护](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/src/api/session-guard.ts)。
- [3.0 beta 绑定升级说明](https://github.com/Tencent/openclaw-weixin/blob/0ad1ae91347a938f52f1e4e2abd6dcda51e05f9e/docs/release-notes-bot-realname-auth_zh_CN.md)：PIN/实名流程；不能绕过平台新增要求。
- [官方包入口](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)。

以上是实现资料，不是独立、稳定的公共 API SLA。服务入口为 `https://ilinkai.weixin.qq.com`，不是飞书 webhook。

## 协议功能地图

| 接口 | 用途 | 本次状态 |
|---|---|---|
| `ilink/bot/get_bot_qrcode`、`get_qrcode_status` | 用户绑定/扫码 | 仅索引；未请求二维码或创建凭据 |
| `ilink/bot/getupdates` | 长轮询、游标推进 | 仅索引；宿主需要持久收件箱与去重 |
| `ilink/bot/sendmessage` | 回复发送 | 仅构造文本 body，不执行 HTTP |
| `ilink/bot/getconfig`、`sendtyping` | 配置与输入状态 | 未实现 |
| `ilink/bot/getuploadurl` + CDN | 加密媒体传输 | 未实现；不接受媒体静默丢失 |

## 本仓库接线与调用

- [离线协议边界](../src/feishu_auth_kit/weixin_ilink.py)：`parse_text_turn`、`ILinkTextTurn.to_dict`、`build_text_reply`。
- [测试](../tests/test_weixin_ilink.py)：纯文本、白名单、账号隔离、消息完整性、uint64 字符串保留、token 不进 runner 序列化、媒体/引用拒绝。
- 现有 `AgentTurnRequest.context` 是 `FeishuMessageContext`；本次**不强转复用**，避免虚构 open_id 或 CardKit 能力。宿主应先消费独立 envelope，再实现通道无关 runner seam。

```python
from feishu_auth_kit.weixin_ilink import parse_text_turn, build_text_reply

# message 来自宿主已认证的 iLink 接收通道；这些 ID 仅为示例。
turn = parse_text_turn(message, account_id="configured-bot", allowed_senders={"configured-owner"})
runner_input = turn.to_dict()  # 没有 context_token；不会自动执行模型或命令
# host_output 来自经过宿主权限检查的处理结果。
body = build_text_reply(turn, host_output, client_id="durable-operation-id")
# body 包含回复能力 token：不得写日志；本函数不会发送。
```

默认拒绝未完成消息、非 USER、群、媒体、引用和未授权 sender；这是本适配器保守子集，不代表平台不支持这些能力。

## 生产前还缺什么

1. 用户正式绑定和平台资格验证；安全凭据存储与日志脱敏，不能在仓库写入 token。
2. 显式 HTTPS 主机白名单、重定向校验、超时/取消；不能盲信服务端 baseurl 后携带凭据访问任意主机。
3. `(account_id, message_id)` 持久去重，消息耐久接收后才推进游标；账号/用户的 context_token 不混用。
4. 过期/拒绝停止，不自动换路；发送结果未知不能盲重发，client_id 不等于已证明服务端幂等。
5. 用户身份、操作权限和发信授权由宿主决定。微信到飞书跨平台转发并未获本模块自动授权。
6. 通道无关 runner 与真实账号端到端收发验收；本次不装依赖、不开服务、不做真实账号联调。

## 验证记录

`PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests -p test_weixin_ilink.py -v`
实际 8 项测试通过（内部含多组拒绝用例）。主 Python 缺 requests，改用已有系统 Python 的 requests 2.32.3；未安装软件。
pytest/ruff 未安装，TypeScript 和全仓测试未运行。离线合同通过 ≠ 微信已接通。

代码独立编写，未复制腾讯实现；上游 MIT 许可证仍见腾讯仓库，不改变本库已有许可证。
