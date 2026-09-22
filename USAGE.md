# 使用手册

> 面向部署和日常运维本工程的人。设计原理见 [ARCHITECTURE.md](ARCHITECTURE.md)，
> 已知问题与改进建议见 [REVIEW.md](REVIEW.md)。

---

## 目录

- [1. 前置要求](#1-前置要求)
- [2. 首次部署](#2-首次部署)
- [3. 部署后验证](#3-部署后验证)
- [4. 控制台日常操作](#4-控制台日常操作)
- [5. 登录 Codex 并确认真的能用](#5-登录-codex-并确认真的能用)
- [6. 在 LiteLLM 中接入模型](#6-在-litellm-中接入模型)
- [7. 配置代理例外](#7-配置代理例外)
- [8. 运维速查](#8-运维速查)
- [9. 排障手册](#9-排障手册)
- [10. 安全注意事项](#10-安全注意事项)

---

## 1. 前置要求

- 一台 Linux 服务器（当前部署在 156 服务器），已装 Docker 与 Docker Compose v2
- 能访问外网（拉镜像、拉订阅、连上游）
- 一个可用的机场订阅链接（已配在 `network/mihomo/config.yaml`）
- 一个 ChatGPT/Codex 账号

网络要求（重要）：

| 方向 | 端口 | 说明 |
| --- | --- | --- |
| 运维浏览器 → 服务器 | `8787` | 控制台，建议限制来源网段 |
| 业务客户端 → 服务器 | `4000` | LiteLLM API |
| 服务器 → 外网 | 任意 | 拉镜像、拉订阅、连上游 |
| 服务器 → 宿主机内网域名 | 见 [7.4](#74-域名只在容器-hosts-里有解析时) | `aix-backup.hismarttv.com` |

**不要**把 7890 / 8317 / 9090 / 1455 映射到宿主机。

---

## 2. 首次部署

### 2.1 准备 `.env`

`.env` 与 `docker-compose.yml` 同目录，Compose 会自动加载。**不要提交到版本库**。

```dotenv
# ---- LiteLLM 网关 ----
# SALT KEY 一旦库里有了数据就永远不能改：它用于加密存储的模型凭据，改了旧记录就解不开了。
# 请单独备份这个值。
LITELLM_MASTER_KEY=sk-<随机长字符串>
LITELLM_SALT_KEY=<随机长字符串，与上面不同>

# ---- Postgres ----
# 注意：POSTGRES_PASSWORD 只在数据目录【首次初始化】时生效。
# 后续修改此值不会改变已有的库密码，反而会让 LiteLLM 连不上数据库
# （pg_isready 健康检查不校验密码，症状是「容器健康但运行时报认证失败」）。
POSTGRES_PASSWORD=<强密码>

# ---- 控制台 ----
CONSOLE_USER=admin
CONSOLE_PASSWORD=<强密码>

# ---- CLIProxyAPI ----
# 数据面 key：LiteLLM 调 http://cli-proxy-api:8317/v1 时用
CLIPROXY_API_KEY=<随机长字符串>
# 管理面 key：仅控制台调 /v0/management 用。必须与上面不同！
CLIPROXY_MANAGEMENT_KEY=<另一组随机长字符串>

# ---- SearXNG 搜索 ----
# SearXNG 自己的 secret_key。entrypoint 会在容器启动时拿它替换 settings.yml 里的
# `ultrasecretkey` 字面量。不设会退化成 ultrasecretkey 并在日志里告警。
SEARXNG_SECRET=<随机长字符串>
# ⚠️ 上游代理地址【不在这里配】：SearXNG 用不带自定义 constructor 的 yaml.safe_load
#    读 settings.yml，引用不了环境变量（写 !ENV 会让容器启动即崩）。
#    要改代理直接改 searxng/settings.yml 的 outgoing.proxies，然后 restart searxng。
#    要直连就把那两行注释掉。

# ---- MCP 搜索端点 ----
# ⚠️ 这是【服务端内部凭据】，不是用户密钥：只给「LiteLLM 网关 → searxng-mcp」这一跳用，
#    存在网关的注册记录里。客户端不再需要它——用户搜东西用的是他自己的 LiteLLM key。
#    不设这个，docker compose up 直接报错退出。
MCP_SEARXNG_TOKEN=<随机长字符串>

# ---- MCP 端点的 Host 白名单 ----
# 网关以容器身份经 compose 网络来连，Host 头就是 searxng-mcp:8090。
# ★ 用【服务名】不要用 IP：服务名在任何环境下都一样，所以这一项不含环境相关的配置，
#   换机器、换 IP、换环境都不用改。漏改的症状是网关那跳报 unhealthy，而 searxng-mcp
#   自己的 /health 照样是绿的（它不受这层限制）——指错方向。
#   这一项和 searxng/register_mcp.py 注册的 URL 必须同源，那个脚本每次运行都会核对。
MCP_SEARXNG_ALLOWED_HOSTS=searxng-mcp:8090
MCP_SEARXNG_ALLOWED_ORIGINS=http://searxng-mcp:8090

# 固定镜像版本用（默认都取 latest）
#SEARXNG_IMAGE=searxng/searxng:latest
#MCP_SEARXNG_IMAGE=isokoliuk/mcp-searxng:latest
```

生成随机值：

```bash
openssl rand -hex 32
```

三个「必须」：

1. 六把钥匙**互不相同**（尤其 `CLIPROXY_API_KEY` ≠ `CLIPROXY_MANAGEMENT_KEY`，
   以及 `MCP_SEARXNG_TOKEN` 不要复用别的值——虽然它现在只留在服务端，但它守的是
   「能抓任意 URL」的那个工具）
2. 都不要填成 `LITELLM_MASTER_KEY`
3. 文件权限收紧：`chmod 600 .env`

> ℹ️ 当前仓库里的 `.env` 存在两处和上述规范不一致的地方，见
> [REVIEW.md](REVIEW.md#p0-1-litellm_master_key-与-postgres_password-同值) 和
> [REVIEW.md](REVIEW.md#p1-5-env-注释与-postgres-实际密码不符)。
> **本次不擅自改动正在运行的密钥**，只在文档里标出，等你确认后再处理。

### 2.2 启动

```bash
cd <项目目录>
docker compose pull
docker compose up -d
docker compose ps
```

七个服务应全部运行（`proxy`、`cli-proxy-api`、`proxy-console`、`litellm`、`db`、
`searxng`、`searxng-mcp`）。`proxy` 可能显示 `unhealthy`——这只是 healthcheck 命令在
Alpine/busybox 上的参数差异，不影响实际转发（其他服务只依赖 `service_started`）。
处理办法见 [9.4](#94-proxy-显示-unhealthy)。

`searxng-mcp` 会等 `searxng` 健康后才起，所以它启动最慢，`up -d` 之后等十几秒再看 `ps`。

---

## 3. 部署后验证

按顺序做这四步，每步都对应一条命令。

### 3.1 确认 mihomo 能出网

必须**显式指定代理**——LiteLLM 容器不设代理环境变量，不能拿它当探针。
用 `proxy-console` 容器（自带 python，且和 `proxy` 同网络）：

```bash
docker compose exec proxy-console python3 -c "
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler(
    {'http': 'http://proxy:7890', 'https': 'http://proxy:7890'}))
print(opener.open('https://api.openai.com/v1/models', timeout=20).status)
"
```

返回 `401` 就说明**通了**——已到达 OpenAI，只是没带 API key。

### 3.2 确认某个上游域名走的是节点还是直连

利用 fake-ip 机制判断：拿到 `198.18.x.x` 说明走节点，拿到真实 IP 说明直连。

```bash
docker compose exec proxy-console python3 -c "
import json, urllib.request
url = 'http://proxy:9090/dns/query?name=chatgpt.com&type=A'
print(json.dumps(json.load(urllib.request.urlopen(url, timeout=10)), ensure_ascii=False))
"
```

> 这是近似手段（`respect-rules: true` 下 DNS 判定与规则判定一致，但最终以实际请求为准）。

### 3.3 确认内网地址没有被代理影响

```bash
docker compose exec litellm python -c "import httpx; print(httpx.get('http://aix-backup.hismarttv.com/', timeout=15).status_code)"
```

### 3.4 确认搜索能用（MCP）

搜索走的是**客户端侧**的 MCP，但服务端要先把后端注册进网关。**分三步，别跳步**：

```bash
# 第一步：注册进网关（幂等；服务器上做一次即可，重启不丢）
python searxng/register_mcp.py --dry-run    # 先看要发什么，token 打码
python searxng/register_mcp.py

# 第二步：★ 确认网关→searxng-mcp 这一跳真的通。status 必须是 healthy。
python searxng/register_mcp.py --check

# 第三步：用一个【普通用户的 key】走网关真握手（不要用 master key，用 master 验等于没验）
USER_KEY='sk-某个普通用户的key'
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://<网关>:4000/searxng/mcp \
  -H "Authorization: Bearer $USER_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

`200` 才算过。**第二步报 `unhealthy` 几乎总是 `MCP_HTTP_ALLOWED_HOSTS` 里没有
`searxng-mcp:8090`**——注意此时 `searxng-mcp` 自己的 `/health` 仍然可能是绿的，指错方向。
`404 MCP server ... not found` 是没注册（回到第一步）。

完整的五层验证（含真搜一次、真读一页正文）见 **[searxng/README.md](searxng/README.md)**。

---

## 4. 控制台日常操作

浏览器打开 `http://<服务器IP>:8787`，用 `.env` 里的 `CONSOLE_USER` / `CONSOLE_PASSWORD` 登录。

### 4.1 页面结构

| 区域 | 能做什么 |
| --- | --- |
| 顶部 | 看当前节点、订阅状态；刷新订阅 / 批量测速 / 重新加载 |
| 筛选栏 | 分组下拉、名称搜索、延迟上限、排序、隐藏超时节点（纯前端过滤，不请求服务端） |
| ChatGPT/Codex 订阅 | 登录、刷新账号、粘贴 OAuth 回调、连通性自检、实际生成一次 |
| 代理例外 | 增删直连/强制代理规则、查看 `NO_PROXY` 片段、重新应用 |
| 节点表格 | 列出节点（序号/名称/类型/延迟/当前），点击切换 |

页面每 30 秒自动刷新一次状态。

### 4.2 切换节点

1. 顶部分组下拉选「🚀 节点选择」（或其他分组）
2. 在表格里按延迟排序，挑一个延迟低的
3. 点该行的「切换」

切换立即生效，不需要重启任何容器。

### 4.3 批量测速

点顶部「批量测速」，对当前分组的全部节点测一遍延迟。
服务端超时上限 120 秒，节点多时会等得比较久（页面按钮会禁用）。

### 4.4 刷新订阅

点顶部「刷新订阅」，让 mihomo 重新拉取机场订阅。
正常情况下 mihomo 每 24 小时自动刷新一次，手动刷新用于刚换了订阅或加了节点的情况。

订阅节点全部来自 `proxy-providers`（而非内联 `proxies:`），
这样服务器端节点列表与桌面客户端保持一致。

---

## 5. 登录 Codex 并确认真的能用

### 5.1 登录

1. 在「ChatGPT/Codex 订阅」面板点「登录 Codex」
2. 控制台向 CLIProxyAPI 要一个一次性 OAuth URL，并自动打开新窗口
3. 在 OAuth 页面完成 ChatGPT 登录和授权
4. 若回调能直接回到 CLIProxyAPI，页面会自动轮询并显示「已登录」

**若浏览器阻止了弹窗**：把按钮返回的登录地址复制到新标签页打开即可，登录状态仍由控制台轮询。

### 5.2 远程部署的回调兜底（最常见的情况）

服务器在远端时，OAuth 回调地址往往指向**你电脑的** `localhost:1455`，服务器收不到。

处理办法：

1. 在浏览器授权完成后，地址栏会停在一个 `http://localhost:1455/...` 的 URL
2. **复制这个完整 URL**
3. 粘贴到控制台的「OAuth 回调地址」输入框，点提交
4. 控制台会把它转发给 CLIProxyAPI 的 `/v0/management/oauth-callback`

> ⚠️ **不要把 1455 端口映射到公网**。手动贴回是安全的做法。

### 5.3 确认登录真的生效（重要）

网页显示「已登录」**只说明 OAuth 回调走完了，不说明凭据能用**。
账号可能已过期、节点可能挂了、账号可能没额度——这些都要实际发一个请求才看得出来。

面板下有两个按钮，**两个都要点**：

| 按钮 | 做了什么 | 证明了什么 | 不证明什么 |
| --- | --- | --- | --- |
| **连通性自检** | 调 `GET /v1/models` + 管理面 `/auth-files` | CLIProxyAPI 活着、数据面 key 对、有凭据文件 | ❌ **不代表能用** |
| **实际生成一次** | 通过 `/v1/chat/completions` 发一个 `ping`（`max_tokens: 8`） | ✅ 这才是"真的能用"的证据 | — |

模型名留空时会自动挑一个带 `codex` 的；自检成功后会把挑中的模型名写回输入框，
可以针对某个具体模型重测。

结果按步骤显示，每一步都贴出上游返回的**原文**——排障时那句原始报错比任何猜测都有用。

结果对照表：

| 现象 | 含义 | 怎么办 |
| --- | --- | --- |
| 第一步 401 | `CLIPROXY_API_KEY` 与 CLIProxyAPI 的 `api-keys` 不一致 | 检查 `.env`，重启 `cli-proxy-api` 和 `proxy-console` |
| 第一步通、第二步说"没有已保存的账号" | 登录没真正落到 `cliproxy_auths` 卷 | 重新登录 |
| 两档都通过 | 链路完全正常 | — |
| 一档过、二档报上游错误 | 凭据或网络问题 | 看原文：多半是 token 过期、节点不通或账号没额度 |
| 二档报不支持 `chat/completions` | 该模型只提供 Responses API | 换一个模型测 |

> 自检走的是容器内部地址，验证的是「控制台 → CLIProxyAPI」这一段。
> 「CLIProxyAPI → mihomo → 节点 → 上游」那一跳由「实际生成」间接证明：
> 生成成功就说明那一跳通了，失败则原文里会带上游的报错。

---

## 6. 在 LiteLLM 中接入模型

先确认 CLIProxyAPI 已有可用的 Codex 账号（做完第 5 节），再操作。

### 6.1 通过管理界面添加

浏览器打开 `http://<服务器IP>:4000`，用 `LITELLM_MASTER_KEY` 登录，新增一个 OpenAI 兼容模型：

| LiteLLM 字段 | 填写 |
| --- | --- |
| Provider | `openai` |
| API Base / Base URL | `http://cli-proxy-api:8317/v1` |
| API Key | `.env` 中的 `CLIPROXY_API_KEY` |
| Model | 从下面的命令输出里选，**不要臆填版本号** |

### 6.2 先看有哪些模型可用

```bash
docker compose exec litellm python -c "
import httpx
r = httpx.get('http://cli-proxy-api:8317/v1/models',
              headers={'Authorization': 'Bearer <CLIPROXY_API_KEY>'}, timeout=15)
print(r.status_code); print(r.text[:1000])
"
```

### 6.3 客户端调用

客户端只认 LiteLLM 的地址，不需要知道 CLIProxyAPI 或 mihomo 的存在：

```bash
curl http://<服务器IP>:4000/v1/chat/completions \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model": "<你在 LiteLLM 里配的模型名>", "messages": [{"role":"user","content":"hi"}]}'
```

### 6.4 私有模型必须放开 `reasoning_effort` 并丢弃 `client_metadata`

**每新增一个自有的 OpenAI 兼容 endpoint，都要做这两步**，否则用 Codex 调它必定失败。
两者独立：只做第一个，400 会变成 500。

Codex 0.150 起只发 `POST /v1/responses`，**每轮都带 `reasoning: {"effort": ...}` 和一个
`client_metadata` 对象**（session/turn id、沙箱模式等遥测）。这两个字段对
`custom_openai` 都是致命的，原因各不相同：

**(a) `reasoning_effort` → 400。** LiteLLM 把 `reasoning.effort` 翻译成
`reasoning_effort`，而 `custom_openai` 没声明支持它，请求在出网关前就被拒：

```
litellm.UnsupportedParamsError: custom_openai does not support parameters:
['reasoning_effort'], for model=<模型名>
```

**(b) `client_metadata` → 500。** LiteLLM 把它**原样透传**给 OpenAI SDK，而
`AsyncCompletions.create()` 没有这个关键字参数：

```
litellm.InternalServerError: Custom_openaiException -
AsyncCompletions.create() got an unexpected keyword argument 'client_metadata'
```

两者的现象一样，Codex 里都只看到 `ERROR: Reconnecting... 1/5`，很容易误判成网络问题。
注意 `reasoning.effort` 填 `"none"` 也一样 400，改档位绕不过去。

**解决办法**——在模型定义里允许前者、丢弃后者：

| LiteLLM 字段 | 填写 |
| --- | --- |
| `litellm_params.allowed_openai_params` | `["reasoning_effort"]` |
| `litellm_params.additional_drop_params` | `["client_metadata"]` |

`additional_drop_params` 让 LiteLLM 在调 provider 前丢掉指定字段。丢的是 Codex 自己的
遥测信封，不是模型输入，没有损失。

从能连到网关的机器上一条命令改好（也可以直接在 `http://<服务器IP>:4000` 的模型编辑页里加）：

```bash
# 用 vscode/ 下的工具生成/执行（会带上该模型完整的 litellm_params）
cd vscode
python private_api.py --emit-gateway-config      # 只打印 curl，先看一眼
python private_api.py --apply-gateway-config     # 确认后直接改
```

> ⚠️ `POST /model/update` 是**整体替换** `litellm_params`。手工发请求时务必把该模型原有的
> `api_base`、`custom_llm_provider` 等一起带上，只发新增的那个键会把模型改坏。

验证（期望 200）：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST http://<服务器IP>:4000/v1/responses \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"<模型名>","input":"hi","reasoning":{"effort":"high"},
       "client_metadata":{"x-codex-turn-metadata":"{}"},
       "allowed_openai_params":["reasoning_effort"]}'
```

> `client_metadata` 故意留在 body 里：它是 `additional_drop_params` 要负责丢掉的字段，
> 网关侧丢了才说明配置生效。

---

## 7. 配置代理例外

### 7.1 先想清楚用哪个清单

| 目标 | 用哪个 |
| --- | --- |
| 这个域名**不该走节点** | **直连例外**（`CustomDirect.list`） |
| 这个域名被误判成国内、**必须走节点** | **强制代理**（`CustomProxy.list`） |

记住：分流的最小粒度是**域名**。上游域名相同的两个模型，无法分开代理。

### 7.2 网页操作

1. 在「代理例外」面板选清单（直连例外 / 强制代理）
2. 选匹配方式：完整域名 / 域名后缀 / 域名关键字 / IPv4 网段 / IPv6 网段 / GEOIP
3. 填值，点「添加并生效」

便利之处：粘贴整条 URL、带端口、带 `*` 前缀都会自动只取域名；IP 段会自动补 `no-resolve`。
含空格、逗号、换行的输入会被拒绝——这类字符会让整个规则集解析失败。

### 7.3 加了规则却不生效？按顺序查

1. **「已加载 N 条」还是「未加载」？**
   - 「未加载」→ mihomo 里没有这个 rule-provider。确认 `network/mihomo/config.yaml`
     声明了 `CustomDirect`，然后 `docker compose restart proxy`
   - 「已加载 0 条」但列表里明明有 → 点「重新应用」
2. **点「重新应用」时如果提示降级成了「整份重载配置」** → 当前 mihomo 版本没有单独刷新
   规则集的接口，配置重载会**重置节点选择**，请顺手确认「当前节点」。
3. **都加载了还是走代理** → 命中的是别的规则。两个例外规则集必须在 `rules:` 最前面，
   检查它们有没有被上面新增的规则挤下去（见 [ARCHITECTURE.md](ARCHITECTURE.md#73-规则优先级)）。
4. **域名解析不出来** → 见下一节。

### 7.4 域名只在容器 hosts 里有解析时

有些域名只在某个容器的 `/etc/hosts` / `extra_hosts` 里有解析，
把请求交给 mihomo 反而解析不出来。本工程的 `aix-backup.hismarttv.com`（指向宿主机）就是这类。

这类域名必须让容器自己直连：

1. 控制台面板底部有一个现成的 `NO_PROXY` 片段，复制出来
2. 覆盖 `docker-compose.yml` 里 `litellm` 服务的 `NO_PROXY`
3. `docker compose up -d litellm`

### 7.5 手工改 `.list` 文件的注意事项

- 控制台重写文件时会**保留文件开头的注释块**，但**规则行之后的注释会被丢弃**
  → 说明统一写在文件开头
- 手工改完记得在网页上点「重新应用」

---

## 8. 运维速查

### 8.1 常用命令

```bash
# 状态
docker compose ps
docker compose logs -f cli-proxy-api proxy proxy-console litellm

# 重启单个服务
docker compose restart proxy              # 改过 mihomo config.yaml 后
docker compose up -d litellm              # 改过 litellm 的 NO_PROXY 后
docker compose restart cli-proxy-api proxy-console   # 轮换 CLIPROXY_* 密钥后
docker compose restart searxng           # 改过 searxng/settings.yml 后
docker compose up -d searxng-mcp         # 改过 MCP_SEARXNG_* 变量后（要重建才重读 .env）
python searxng/register_mcp.py           # 改过 MCP_SEARXNG_TOKEN 或 searxng-mcp 地址后

# 看实际生效的 compose 配置（排错变量替换问题）
docker compose config
```

### 8.2 备份

```bash
# 数据库
docker compose exec db pg_dump -U litellm litellm > backup_$(date +%F).sql

# OAuth 凭据卷
docker run --rm -v litellm-0914-deploy_cliproxy_auths:/data -v "$PWD":/out \
  alpine tar czf /out/cliproxy_auths_$(date +%F).tar.gz -C /data .

# .env 里的 LITELLM_SALT_KEY 必须单独备份（改了旧记录就解不开了）
```

卷名前缀取决于 compose 项目名，用 `docker volume ls` 确认真实名称。

### 8.3 升级

```bash
docker compose pull
docker compose up -d
```

CLIProxyAPI 镜像版本通过 `.env` 的 `CLI_PROXY_IMAGE` 覆盖，
默认值是 `docker-compose.yml` 里的 `eceasy/cli-proxy-api:v7.3.2`。

### 8.4 ⚠️ 危险操作

| 命令 | 后果 |
| --- | --- |
| `docker compose down -v` | **删除全部命名卷**：OAuth 凭据、Postgres 数据全没 |
| 改 `POSTGRES_PASSWORD` 后重启 | 已有数据卷不会改密码，LiteLLM 会认证失败 |
| 改 `LITELLM_SALT_KEY` | 库里的模型凭据再也解不开 |

---

## 9. 排障手册

### 9.1 `cli-proxy-api` 启动后立即退出

```bash
docker compose config                    # 确认变量都替换成功
docker compose logs --tail=100 cli-proxy-api
```

多半是 `.env` 里 `CLIPROXY_API_KEY` 或 `CLIPROXY_MANAGEMENT_KEY` 缺失/为空。

### 9.2 控制台显示 CLIProxyAPI 不可用

```bash
docker compose logs --tail=100 cli-proxy-api proxy-console
docker compose exec proxy-console python3 -c "
import urllib.request, os
print(urllib.request.urlopen(
    os.environ['CLIPROXY_MANAGEMENT_URL'] + '/auth-files',
    headers={'Authorization': 'Bearer ' + os.environ['CLIPROXY_MANAGEMENT_KEY']},
    timeout=5).status)
"
```

- 返回 `401` → 两边 `CLIPROXY_MANAGEMENT_KEY` 不一致
- 连接失败 → 检查服务名是否仍是 `cli-proxy-api`、容器是否在运行

### 9.3 Codex 登录停在「等待 OAuth 回调」

见 [5.2](#52-远程部署的回调兜底最常见的情况)。远程 Docker 环境的标准解法是把回调 URL 贴回控制台。

```bash
docker compose logs -f cli-proxy-api proxy-console
```

### 9.4 `proxy` 显示 `unhealthy`

Alpine 的 busybox `wget` 参数和 GNU wget 不完全一致。

确认 mihomo 日志没有退出后，可以：

- 删除 `docker-compose.yml` 里 `proxy.healthcheck` 段，或
- 按宿主机镜像版本调整参数

其他服务只依赖 `service_started`，这个状态标记不影响转发。

### 9.5 mihomo TLS / x509 错误

当前挂载 `/etc/ssl/certs:/etc/ssl/certs:ro`。宿主机若是 RHEL/CentOS 系，改为 `/etc/pki:/etc/pki:ro`：

```yaml
volumes:
  - /etc/pki:/etc/pki:ro
```

然后 `docker compose restart proxy cli-proxy-api`。

### 9.6 订阅刷新接口返回 404

这是 mihomo 版本差异。节点查看、筛选、切换仍可用。

```bash
docker compose logs proxy-console
docker compose restart proxy     # 触发启动时重新拉取订阅
```

### 9.7 网页上加了例外，但流量还是走代理

见 [7.3](#73-加了规则却不生效按顺序查)。

### 9.8 客户端调用 500 / 上游报错

按链路逐段定位：

```bash
# ① LiteLLM → CLIProxyAPI
docker compose exec litellm python -c "
import httpx
r = httpx.get('http://cli-proxy-api:8317/v1/models',
              headers={'Authorization': 'Bearer <CLIPROXY_API_KEY>'}, timeout=15)
print(r.status_code, r.text[:500])
"

# ② CLIProxyAPI → mihomo → 上游（用控制台「实际生成一次」最直观）
docker compose logs -f cli-proxy-api
```

### 9.9 搜索用不了

**先记住一件事：`searxng-mcp` 的 `/health` 绿了什么都不代表**——它是官方免鉴权端点，且不会去碰
SearXNG。所以别拿它当证据。按下面这张表定位：

| 症状 | 最可能的原因 | 怎么办 |
| --- | --- | --- |
| `--check-search` 报 **401** | 搜索那步跑在模型配置之前，或用了不同的 `--api-key` | 客户端用的是**你自己的 LiteLLM key**，没有第二个搜索 key。重跑主流程命令即可 |
| `--check-search` 报 **404** `MCP server ... not found` | 网关不知道这个 MCP server（没注册）或 URL 带了 `/v1` | 在网关那台机器上跑 `python searxng/register_mcp.py` |
| `register_mcp.py --check` 报 **unhealthy**，但 searxng-mcp 的 `/health` 是绿的 | `MCP_HTTP_ALLOWED_HOSTS` 里没有 `searxng-mcp:8090`（**连端口精确比对**） | 检查 `.env` 那一项；脚本每次运行都会核对并报警 |
| `--check-search` 提示 **配置的 URL 与网关端点不一致** | 客户端配置是旧地址（网关换过地址/端口） | 重跑主流程命令，它会重写 |
| 客户端 403 | 网关自身的策略（key 范围 / 允许的模型） | 这层不再是 Host 白名单——客户端只跟网关说话 |
| 握手能过，但一搜就失败 | SearXNG 自己搜不出来 | 见下 |
| Claude Code 的 `/mcp` 里没有 `searxng` | 写错文件或漏了 `type` | 必须是 `~/.claude.json` 且 `"type": "http"`，见 [vscode/README.md](vscode/README.md#搜索web-search) |
| `searxng` 容器 `Restarting (1)`，`searxng-mcp` 报 `dependency failed to start` | `settings.yml` 里写了 `!ENV` 或任何自定义 YAML 标签 | SearXNG 用裸 `yaml.safe_load` 读它，会 `ConstructorError` 崩溃。改成硬编码值，见 [searxng/README.md](searxng/README.md) |

> `searxng` 启动失败时 `docker inspect` 会给出一个很有迷惑性的组合：`State.Health` 是
> `unhealthy`，但 `FailingStreak: 0` 且 `Log: []`。**空 Log 说明 healthcheck 一次都没跑过**——
> 是应用在启动阶段就死了。别去调 healthcheck 命令，直接看 `docker compose logs searxng`。

SearXNG 自己搜不出来时，进容器直接问它（它不发布端口）：

```bash
docker compose exec searxng wget -qO- 'http://127.0.0.1:8080/search?q=test&format=json' | head -c 400
```

返回 **403 或一页 HTML** → `searxng/settings.yml` 的 `search.formats` 里丢了 `json`。
返回空或超时 → 上游引擎出不去，看 `docker compose logs --tail=30 searxng`，多半是 `proxy`
或节点规则的问题（和 CLIProxyAPI 出网是同一条链路）。

完整的四层验证见 [searxng/README.md](searxng/README.md)。

---

## 10. 安全注意事项

| 项 | 要求 |
| --- | --- |
| 端口暴露 | 只发布 8787 和 4000；**不要**额外发布 7890 / 8317 / 9090 / 8080 / 8090 / 1455 |
| 8090（MCP 后端）访问控制 | **不发布端口**，只在 compose 网络里可见。入口是网关的 `/searxng/mcp`，用 LiteLLM virtual key 鉴权（`allow_all_keys: true`），所以「谁能调」由网关的 key 体系决定，不再靠网络边界。`MCP_SEARXNG_TOKEN` 只是服务端内部凭据 |
| 搜索工具的暴露面 | `web_url_read` 能抓**任意 URL**，等于一个出网抓取器。它现在挂在网关后面，所以放行等于「任何持有合法 key 的人都能用」——发 key 前想清楚这一点 |
| 8787 访问控制 | 强 `CONSOLE_PASSWORD` + 防火墙限制来源网段，**不要暴露到公网** |
| `.env` | `chmod 600`，不进版本库，单独备份 `LITELLM_SALT_KEY` |
| 密钥轮换 | `CLIPROXY_*` 轮换要同时更新 CLIProxyAPI 与控制台并重启两者；`MCP_SEARXNG_TOKEN` 轮换只要改 `.env` → `docker compose up -d searxng-mcp` → **重跑 `python searxng/register_mcp.py`**。**客户端不用动**——它拿的是自己的 LiteLLM key，跟这个 token 无关 |
| 凭据管理 | `CLIPROXY_MANAGEMENT_KEY` 权限高于模型 API key，**不要发给客户端** |
| 备份访问 | `cliproxy_auths` 卷内的 OAuth JSON 等同账号凭据，备份件要控权限 |
| 发布仓库前 | `network/mihomo/config.yaml`（订阅令牌）和 `network/clash/RihD9ROJzl50.yaml`（节点凭据）**必须先处理**，详见 [REVIEW.md](REVIEW.md) |

> 8787 的权限等价于改 mihomo 配置：能登进去就能决定哪些流量直连、哪些走节点。
> 请按"生产配置面"而不是"内部小工具"的标准来对待它。
