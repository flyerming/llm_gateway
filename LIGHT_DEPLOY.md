# 轻量部署

轻量栈是全功能私有化部署的一个**独立 Compose 方案**，用于只保留下面两条链路：

```text
Codex OAuth
    │
    ▼
CLIProxyAPI:8317  ──proxy-url──▶  mihomo:7890  ──▶  Codex 上游
    │
    └── OpenAI 兼容 /v1 API ──▶  DeepSeek Harness
```

它不启动全功能栈中的 LiteLLM、Postgres、SearXNG 和统一网关。为了让
CLIProxyAPI 仍然可登录 Codex、切换节点和处理远程 OAuth 回调，轻量栈保留了
现有的 `proxy-console`；这不是模型链路的额外依赖，而是运维控制面。

## 1. 与全功能栈隔离

轻量配置文件是：

```text
docker-compose.light.yml
```

它使用：

- Compose 项目名：`litellm-light`；
- 独立 Docker 网络：`litellm-light_network`；
- 独立卷：`litellm-light_cliproxy_auths`、`litellm-light_dsh_data` 等；
- 默认端口：`8788`（控制台）、`3081`（DeepSeek Harness）。

因此可以在全功能栈仍运行时启动轻量栈。若你希望复用全功能栈使用的
`8787/3080` 端口，先停止全功能栈，或者通过 `.env` 覆盖：

```dotenv
LIGHT_CONSOLE_PORT=8787
LIGHT_DSH_PORT=3080
```

不要让两个栈共用 `cliproxy_auths` 或 `dsh_*` 卷。两套部署的 OAuth 会话、
用户认证资料和 Harness 用户数据应分别备份。

## 2. 准备变量

轻量栈沿用仓库根目录 `.env`。至少需要：

```dotenv
CLIPROXY_API_KEY=请设置数据面密钥
CLIPROXY_MANAGEMENT_KEY=请设置管理面密钥
CONSOLE_USER=admin
CONSOLE_PASSWORD=请设置控制台密码
DSH_BOOTSTRAP_USER=admin
DSH_BOOTSTRAP_PASSWORD=请设置 Harness 密码
```

`CLIPROXY_API_KEY` 同时用于：

1. CLIProxyAPI 的 OpenAI 兼容数据面；
2. DeepSeek Harness 的 `DSH_MODEL_API_KEY`；
3. 控制台的数据面连通性自检。

这是轻量栈的**共享数据面 key**，适合内网、少量受信用户场景。若需要按用户计量、
额度和撤销，应使用全功能栈的 LiteLLM virtual key 方案，而不是把同一把
 CLIProxyAPI key 注入所有 DSH 用户进程。

### 模型 ID

轻量栈不能预先假定 CLIProxyAPI 当前账号暴露哪些模型。可以先不设置
`DSH_LIGHT_MODEL_IDS` 启动控制面；此时 DSH 只使用
`__SET_AFTER_CLIPROXY_LOGIN__` 作为引导占位值，**不要用它发起正式对话**。

先启动并登录 Codex，再查看：

```bash
docker compose -f docker-compose.light.yml exec deepseek-harness node -e \
  "fetch('http://cli-proxy-api:8317/v1/models',{headers:{Authorization:'Bearer '+process.env.DSH_MODEL_API_KEY}}).then(async r=>console.log(r.status,await r.text()))"
```

然后在 `.env` 中设置实际的模型 ID（逗号分隔）：

```dotenv
DSH_LIGHT_MODEL_IDS=<从 /v1/models 复制的真实模型 ID>
```

修改后重建 DSH：

```bash
docker compose -f docker-compose.light.yml up -d --force-recreate deepseek-harness
```

`DSH_LIGHT_MODEL_IDS` 只用于新用户第一次生成 `settings.yaml`。已有用户可以在
Harness 的“设置 → 模型”中调整；如果要让已有用户重新采用新的默认清单，需要删除
对应用户的 `settings.yaml` 后重新访问，或者使用内置配置编辑页。

## 3. 启动和更新

首次启动建议先检查 Compose 渲染结果：

```bash
docker compose -f docker-compose.light.yml config
```

构建并启动：

```bash
docker compose -f docker-compose.light.yml up -d --build
docker compose -f docker-compose.light.yml ps
```

只更新 DSH 镜像：

```bash
docker compose -f docker-compose.light.yml up -d --build --force-recreate deepseek-harness
```

查看日志：

```bash
docker compose -f docker-compose.light.yml logs -f proxy cli-proxy-api proxy-console deepseek-harness
```

停止轻量栈：

```bash
docker compose -f docker-compose.light.yml down
```

不要随意使用 `down -v`：它会删除 Codex OAuth 凭据、Harness 用户数据和认证资料。

## 4. 首次使用顺序

### 4.1 登录 Codex 和选择节点

打开：

```text
http://<服务器IP>:8788
```

用 `CONSOLE_USER/CONSOLE_PASSWORD` 登录，在“ChatGPT/Codex 订阅”面板完成 Codex
OAuth。远程部署遇到回调落到你电脑的 `localhost:1455` 时，复制完整回调 URL，粘贴
回控制台提交。

在节点面板选择一个可用节点。控制台里的“连通性自检”只能证明接口和凭据文件存在；
“实际生成一次”才是 CLIProxyAPI → mihomo → Codex 上游整条链路可用的证据。

### 4.2 打开 DeepSeek Harness

打开：

```text
http://<服务器IP>:3081
```

使用 `DSH_BOOTSTRAP_USER/DSH_BOOTSTRAP_PASSWORD` 登录。Harness 容器内的模型 provider
已经预配置为：

```text
http://cli-proxy-api:8317/v1
```

所以模型请求不会经过 LiteLLM，也不需要宿主机暴露 CLIProxyAPI 的 8317 端口。

## 5. 排障

### CLIProxyAPI 能启动但 `/v1/models` 失败

先在控制台完成 OAuth，再点击“实际生成一次”。常见原因：

- `CLIPROXY_API_KEY` 改过但没有重建 `cli-proxy-api` 和 `deepseek-harness`；
- mihomo 没有可用节点；
- Codex OAuth 回调没有真正落入 `light_cliproxy_auths` 卷；
- `network/mihomo/config.yaml` 的订阅或例外规则有问题。

改 key 后执行：

```bash
docker compose -f docker-compose.light.yml up -d --force-recreate \
  cli-proxy-api proxy-console deepseek-harness
```

### Harness 页面能打开但模型请求失败

确认 `.env` 中的 `DSH_LIGHT_MODEL_IDS` 与 `/v1/models` 返回的 `id` 完全一致，
然后重建 DSH：

```bash
docker compose -f docker-compose.light.yml up -d --build --force-recreate deepseek-harness
```

另外确认不要把 `http://litellm:4000/v1` 写进轻量用户的
`/data/users/<user>/.dsh/settings.yaml`；轻量栈必须使用
`http://cli-proxy-api:8317/v1`。

### 只想运行轻量栈，不想让它与全功能栈同时存在

停止全功能栈即可：

```bash
docker compose -f docker-compose.yml down
docker compose -f docker-compose.light.yml up -d --build
```

两套栈的卷不同，停止其中一套不会删除另一套的 OAuth 或用户数据。
