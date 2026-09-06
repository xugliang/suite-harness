# 对象授权、附件接缝和模型能力配置

这三项是可被不同公司产品复用的框架接口。它们不定义客户、销售或其他业务实体；产品实现自身的数据表和归属规则。

## 登录身份与业务对象

`RequestScope.principal_id` 表示服务端认证的操作人。被读取的文档等对象使用 `ResourceRef`，不能把对象 ID 填入操作人字段。产品实现 `ResourceAuthorizer.authorize(scope, resource, action=...)`，从可信数据库查询当前归属，返回严格的 `True` 或 `False`。

```python
from suiteharness.runtime import ResourceRef, require_resource_access

resource = ResourceRef(scope.tenant_id, scope.product_id, "document", document_id)
authorized = await require_resource_access(authorizer, scope, resource, action="read")
```

`require_resource_access` 首先检查公司和产品边界，再调用产品授权器；拒绝、未知对象、授权器错误和超时统一抛出 `ResourceAccessDenied("resource not found")`。取消信号仍向上传播。授权结果 `AuthorizedResource` 只描述本次请求，不是可给浏览器使用的令牌，也不能缓存以跳过之后的归属检查。

数据读取和写入仍需在同一个数据库事务中使用操作人及对象条件，避免在授权后、读取前发生归属变化。仅调用一次授权器不会自动为 SQL、向量检索或文件访问添加过滤条件。

## 附件引用与可信内容解析

浏览器的 `ChannelAttachment` 只包含引用 ID 和客户端媒体描述。`AttachmentResolver.resolve(scope, attachment_id)` 由产品或公司部署实现，读取受控存储并重新检查当前操作人的权限。返回 `ResolvedAttachment`，其中公司、产品、当前操作人、附件 ID 必须与请求一致；`principal_id` 是本次获得读取权限的操作人，不等同于文件最初上传者。

`AttachmentContentBuilder(resolver, authorizer)` 执行以下步骤：

1. 检查数量和重复引用。
2. 以 `ResourceRef(kind="attachment", ...)` 检查读取权限。
3. 调用可信解析器，再检查返回对象的作用域和 ID。
4. 依据实际字节数检查单文件与总量上限，不信任客户端的 `size_bytes`。
5. 将存储声明的 UTF-8 `text/plain` 转成 `TextContent`，将 PNG/JPEG/WebP/GIF 转成内联 `ImageContent`。

默认最多 8 个附件、单个 10 MiB、合计 20 MiB，每个授权和解析步骤默认 15 秒。可在服务端构建器参数中收紧或调整。解析器不得把客户端 ID 当文件路径或 URL；此接口不执行任意网络下载。

```python
from suiteharness.channels import AttachmentContentBuilder

builder = AttachmentContentBuilder(resolver=company_attachment_store, authorizer=object_access)
content_parts = await builder.build(scope, channel_message.attachments)
```

这里得到的是已授权、协议无关的模型内容。当前默认 ReAct 仍只构建文本消息，本变更未启用从 ReAct 到模型的自动附件传递。产品调用模型前需完成明确的模型路由和附件数据使用配置，再在自己的受控工作流中接入；不能把客户端提交的任意 JSON 直接反序列化为可信模型消息。

上传、下载和图片校验属于独立的服务端接口：上传时验证真实图片格式、解码结果、像素上限，存储时保存来源与归属；下载时重新鉴权。`AttachmentContentBuilder` 不代替解码检查，只接受受信存储返回的已验证媒体。证据原件不要放进提供通用 `read/grep/Bash` 的产品工作区。

WebSocket 的 `MessageFrame` 现在允许省略文字而仅携带附件引用；文字和附件都为空时拒绝。大文件仍通过受控 HTTP 上传，WebSocket 传引用和进度；二进制 WebSocket 帧仍不受支持。框架不内置具体上传路由或业务前端。

## 具体模型的能力配置

厂商可提供多个能力不同的模型。`ModelProfile.capabilities` 使用 `ModelCapabilities`，其中没有填写的字段保留 `ProviderDescriptor` 默认值；明确填写 `false` 可以收紧能力。

```yaml
models:
  profiles:
    - profile_id: internal_text
      provider_id: vllm
      model: your-reviewed-text-model
      base_url: http://model.internal/v1
      allow_plain_http: true
      capabilities:
        vision: false
        tools: false
        streaming: false
```

若某厂商的保守默认声明不包含视觉或 JSON Schema，而部署方已验证某个具体模型支持，可以在该 profile 中明确使用 `allow_capability_overrides: true`，并逐项声明能力：

```yaml
capabilities:
  vision: true
  json_schema: true
allow_capability_overrides: true
```

这是服务端管理员配置，不允许从用户消息修改。提升声明默认会使启动校验失败；显式启用覆盖的 profile 只能绑定一个准确模型，不可使用 `allowed_models` 让其他未验证模型继承提升后的能力。声明只确认适配器应允许什么，并不证明实际服务商实现支持；需要由部署者验证该模型。

模型网关检查视觉、文档、工具、结构化输出和流式调用；关闭并行工具时，完整响应或流式响应中出现第二个不同工具调用会被拒绝。`reasoning` 与 `max_context_tokens` 当前是能力元数据，框架没有通用推理开关或精确分词器，不能靠它们声称已经执行精确 token 计数。收紧上下文上限可以声明，提高已知上限同样需要显式覆盖许可。

在线模型和内网离线模型使用相同 profile 结构。选择哪个路由、何种附件可以送往哪个模型，应在产品上线配置中明确；配置能力开关本身不会发起网络请求。

## 浏览器 WebSocket 登录

浏览器的 `new WebSocket(...)` 不能设置任意 `Authorization` 请求头。公司登录成功后调用 `POST /auth/session`，框架设置 `suiteharness_session` 短票 Cookie，再由浏览器自动带入 `wss://.../ws` 握手。既有 HTTP 响应中的 `access_token` 保留给非浏览器客户端；浏览器无需读取或将它存入 localStorage，更不要放入 WebSocket URL。

Cookie 使用 `HttpOnly`（JavaScript 不能读取）、`SameSite=Strict`（只在同站环境发送）、默认 `Secure`（只通过 HTTPS/WSS 发送），不设置 Domain（仅当前主机）。Path 限定配置的 `websocket_path`，默认 `/ws`。寿命使用既有 `session_lifetime_seconds`，默认 300 秒、最多 900 秒。服务器在握手、每个非断开客户端帧前以及空闲连接的周期复核中重新验签；周期由 `session_revalidation_interval_seconds` 控制，默认 30 秒、最大 300 秒且不能超过票据寿命。短票到期或账号状态改变会关闭连接并取消该连接尚在运行的请求，客户端须重新交换公司登录凭据。

```javascript
await fetch('/auth/session', {
  method: 'POST',
  credentials: 'same-origin',
  // 公司 SSO 如果使用 HttpOnly Cookie，不需要额外 Authorization 头。
});
const socket = new WebSocket(`wss://${location.host}/ws`, ['suiteharness.v1']);
```

部署应让前端与接口位于同一站点，推荐同源反向代理。HTTP 登录交换和 WS 握手仍检查准确 Origin 白名单；恶意跨站来源即使提供有效 Cookie 也会被拒绝。设置 `channels.web.session_cookie_name` 可以区分同一主机上的多套部署。`session_cookie_secure: false` 仅供明确的开发环境 HTTP 测试，生产配置强制要求 true。不要通过请求头或传入 Origin 自动关闭 Secure。
