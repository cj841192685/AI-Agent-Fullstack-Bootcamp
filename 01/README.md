# 作业一：LLM 统一模型调用服务

一个面向 Agent Engineering Platform 的统一模型入口。它对上提供 OpenAI Compatible API，对下连接多个 OpenAI-compatible 供应商，并在网关层集中处理模型路由、流式转发、结构化输出、Prompt 模板、用量与成本记录、重试、限流和故障转移。

本项目以课程 `1-7/llm-gateway` 参考工程为基础独立交付，并补充了并发边界、业务内容 TTFT、Prompt 历史版本激活、未知价格成本语义及更完整的失败路径测试。默认使用模拟上游完成测试，不需要真实模型额度。

## 已实现能力

- `POST /v1/chat/completions`：Chat Completions 兼容入口
- `POST /v1/responses`：Responses API 兼容入口
- `GET /v1/models`：返回网关公开的模型别名
- 多供应商、模型别名、优先级路由与加权轮询
- 首选供应商失败后自动重试和 fallback；带轻量熔断器
- SSE 逐块透明转发；客户端断开时取消上游连接
- `response_format.json_schema` / `text.format` 透传及本地 JSON Schema 二次校验
- 非流式结构化输出校验失败后自动把错误反馈给模型修复
- Jinja2 Sandbox Prompt 模板、版本、激活版本和变量渲染
- SQLite 记录 Token、Cost、Latency、TTFT、重试、fallback 和错误
- OpenAI 风格错误对象、Bearer API Key 和进程内令牌桶限流
- Docker、健康检查、OpenAPI 文档和自动化测试

## 架构

```text
Client / Agent
      │ OpenAI SDK / HTTP / SSE
      ▼
FastAPI Compatible Layer
      │ 认证 · 限流 · Pydantic 入参校验
      ▼
Prompt Renderer ──► Model Router ──► Retry / Circuit Breaker
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
           OpenAI    DeepSeek    Local / Other
              │          │          │
              └──────────┼──────────┘
                         ▼
        Structured Output Validator
                         │
                         ├── JSON / SSE response
                         └── SQLite usage ledger
```

## 快速开始

要求 Python 3.10+，推荐 Python 3.12。

```bash
cd AI-Agent-Fullstack-Bootcamp-Assignment/01
cp gateway.example.yaml gateway.yaml
cp .env.example .env
# 编辑 .env 和 gateway.yaml，至少设置一个已启用供应商的 API Key。

python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
GATEWAY_CONFIG=gateway.yaml .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000 --env-file .env
```

Windows PowerShell 的启动命令为：

```powershell
$env:GATEWAY_CONFIG = "gateway.yaml"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000 --env-file .env
```

打开：

- OpenAPI：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>
- 就绪检查：<http://localhost:8000/readyz>

也可直接运行：

```bash
docker compose up --build
```

## 使用 OpenAI SDK

调用方只知道网关密钥，不需要持有任何上游供应商密钥。

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="replace-with-a-long-random-secret",
)

response = client.chat.completions.create(
    model="smart",  # gateway.yaml 中的公开别名
    messages=[{"role": "user", "content": "解释什么是 Agent Loop"}],
    temperature=0.2,
    max_tokens=800,
)
print(response.choices[0].message.content)
```

### Streaming

```python
stream = client.chat.completions.create(
    model="fast",
    messages=[{"role": "user", "content": "写一个 FastAPI SSE 示例"}],
    stream=True,
    stream_options={"include_usage": True},
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

网关原样转发上游 SSE。若首个数据块到达前失败，可以安全重试或切换供应商；一旦已有内容发送，网关不会从另一模型续写，以免产生重复或语义错乱，而是发送一个 SSE 错误事件并结束。客户端断开后，网关会关闭上游响应，避免继续消耗 Token。

如业务确实需要在断线后找回已生成的长文本，可在配置中开启 `stream_checkpoint.enabled`。网关会按时间间隔批量保存已解析的文本增量，客户端可用响应头中的 `X-Request-ID` 查询：

```bash
curl http://localhost:8000/v1/streams/req_xxx/checkpoint \
  -H 'Authorization: Bearer replace-with-a-long-random-secret'
```

检查点状态为 `streaming`、`success`、`error` 或 `cancelled`。此能力默认关闭，因为它会持久化模型正文；开启前应先确定加密、权限和数据保留策略。检查点解决网络断线后的内容找回，不会突破模型自身的 Context Window；超长任务仍应由上层 Agent 做分段、摘要或检索式续写。

### Responses API

```bash
curl http://localhost:8000/v1/responses \
  -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  -H 'Content-Type: application/json' \
  -d '{"model":"smart","input":"用三点解释模型路由"}'
```

路由目标必须在 `gateway.yaml` 中声明 `api: responses` 或 `api: both`。网关按协议透明代理，不会把不支持 Responses API 的 Chat Completions 供应商强行伪装成 Responses API。

## Structured Output

Chat Completions 示例：

```python
response = client.chat.completions.create(
    model="smart",
    messages=[{"role": "user", "content": "提取：Ada，36岁"}],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "person",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
                "required": ["name", "age"],
                "additionalProperties": False,
            },
        },
    },
)
```

处理链路是：Schema 传给上游模型 → 获取响应 → 本地 `jsonschema` 再校验 → 失败时附带准确校验错误让模型修复 → 达到重试上限后返回 HTTP 422。流式输出会透传原生 Schema，但无法在已经发送内容后进行无损纠错，因此严格业务协议建议使用非流式接口。

## Prompt 模板与版本

新建或发布一个版本：

```bash
curl http://localhost:8000/v1/prompts \
  -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"code-reviewer",
    "name":"代码审查助手",
    "role":"system",
    "content":"你是 {{ language }} 代码审查助手。重点关注 {{ focus }}。",
    "activate":true
  }'
```

在模型请求中引用，无需把完整 Prompt 传给上层应用：

```json
{
  "model": "smart",
  "messages": [{"role": "user", "content": "请审查以下代码……"}],
  "gateway_prompt": {
    "id": "code-reviewer",
    "variables": {"language": "Python", "focus": "并发安全"}
  }
}
```

再次以同一 `id` 调用 `POST /v1/prompts` 会创建新版本。`activate=true` 会将它设为当前版本；请求也可以显式指定 `version`。调用 `POST /v1/prompts/{id}/versions/{version}/activate` 可以回滚并激活历史版本。模板采用 Jinja2 Sandbox 和严格变量检查，缺失变量会在调用上游前返回 422。

Prompt 模板可以降低重复和配置漂移，但不能从根本上消除 Prompt Injection。生产应用仍应隔离不可信输入、限制工具权限、校验工具参数，并把模型输出当作不可信数据处理。

## 模型路由配置

```yaml
providers:
  vendor_a:
    base_url: https://api.vendor-a.example
    api_key: ${VENDOR_A_API_KEY}
  vendor_b:
    base_url: https://api.vendor-b.example
    api_key: ${VENDOR_B_API_KEY}

models:
  coding:
    strategy: priority
    routes:
      - provider: vendor_a
        model: provider-a-model-id
        api: both
      - provider: vendor_b
        model: provider-b-model-id
        api: chat
```

客户端请求 `coding`，网关把它改写成相应的真实模型 ID。`priority` 按配置顺序尝试；`weighted_round_robin` 用 `weight` 分配首选路由，并保留其他路由作为 fallback。重试只覆盖网络错误、超时和配置中的状态码；普通 4xx 不重试。

当前熔断器和限流器是单进程内存实现。部署多个副本时，应把状态替换为 Redis 等共享存储；接口边界已独立在 `app/services/router.py` 和 `app/core/rate_limit.py`。

## 用量与成本

查询最近调用：

```bash
curl 'http://localhost:8000/admin/usage?limit=20' \
  -H 'Authorization: Bearer replace-with-a-long-random-secret'
```

每条记录包括：

- 请求 ID、时间、入口协议、调用方密钥指纹
- 公开模型、供应商、真实模型
- 输入/输出/缓存 Token 和按配置计算的美元成本
- 总延迟、流式首 Token 延迟（TTFT）
- 重试次数、fallback 次数、状态和错误类型
- 使用的 Prompt ID 与版本

成本取决于 `pricing` 配置，未配置价格时记录为 `null`，避免把“未知”误报成真实零成本。流式 TTFT 从请求开始计时到首个非空业务文本增量，不把空角色块当作有效内容。流式请求需要上游返回 usage 才能准确记录 Token；调用方应传 `stream_options: {"include_usage": true}`。API Key 只记录不可逆短指纹，不记录原值；Prompt 与用户消息正文也不会写入用量表。

## 错误语义

错误统一为 OpenAI 风格：

```json
{
  "error": {
    "message": "Unknown model alias: demo",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

- `401`：网关 API Key 无效
- `404`：模型别名或 Prompt 不存在
- `422`：请求校验失败、模板渲染失败或结构化输出不合规
- `429`：网关限流或上游限流且无法 fallback
- `502/503`：上游失败或没有健康路由
- 流开始后的错误：SSE `data: {"error": ...}`，随后 `[DONE]`

## 项目结构

```text
app/
├── api/routes.py          # 兼容 API、Prompt 与管理接口
├── core/                  # 错误、鉴权、限流
├── services/
│   ├── gateway.py         # 调用编排、流式、重试、结构化纠错
│   ├── upstream.py        # OpenAI-compatible 上游 HTTP 客户端
│   ├── router.py          # 路由、加权、fallback、熔断
│   ├── prompts.py         # Prompt 版本和安全渲染
│   ├── structured.py      # JSON 提取与 Schema 校验
│   └── usage.py           # Token、Cost、Latency 账本
├── config.py              # YAML + 环境变量配置
├── schemas.py             # Pydantic 入参/出参模型
└── main.py                # FastAPI 生命周期与组装
```

## 验证

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check app tests
docker build -t unified-llm-gateway .
```

本次本地验证结果：**16 项测试通过，Ruff 静态检查通过，示例 YAML 加载通过**。测试使用内存 Mock Transport 模拟上游，不消耗真实模型额度，覆盖路由回退、重试、结构化纠错及修复上限、Prompt 严格变量与版本回滚、SSE 首块后故障、断连取消、Responses 能力路由、鉴权、限流、并发边界、用量与未知价格语义。

当前执行环境未安装 Docker，因此 Dockerfile 与 Compose 已完成配置检查，但**镜像构建和容器启动仍需在有 Docker 的环境执行**。真实供应商联调也需要调用方自行配置有效密钥后完成。

## 已知边界

- 对外兼容 OpenAI，但当前主要原样转发上游 SSE，并未把所有供应商事件转换为完整的内部统一事件协议。
- 限流、并发与熔断是进程内实现；多副本需要共享状态，SQLite 也应按规模迁移。
- Gateway 不执行模型请求中的工具，也不决定 Agent 业务任务终态；工具权限和业务校验属于 Agent Harness。
- 提交前应登录课程官方详细要求文档逐项复核，并按其要求补充仓库信息。

## 生产化建议

- 密钥放入 Secrets Manager/KMS，不把 `.env` 或 `gateway.yaml` 中的真实密钥提交到仓库。
- 在公网入口增加 TLS、WAF、审计与租户级配额；管理接口最好拆分到内网。
- 多副本部署时将限流、熔断和动态路由状态迁移到 Redis。
- 高吞吐场景将 SQLite 换为 PostgreSQL/ClickHouse，并异步批量写入用量事件。
- 按供应商能力维护路由，尤其是 Responses API、JSON Schema、工具调用和 context window。
- 对敏感数据增加脱敏、区域路由、数据保留期限与供应商 DPA 策略。
