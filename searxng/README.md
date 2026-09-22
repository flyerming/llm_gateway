# SearXNG + MCP：把「搜索」放回模型手里

这一组两个服务（`searxng` + `searxng-mcp`）只解决一件事：**让私有网关上的模型能用搜索**。

## 为什么必须放在客户端侧

服务端没有开关可配。Codex 的 web search 是**托管工具**——它只是把 `{"type":"web_search"}` 塞进
Responses API 的 `tools` 数组，指望上游去执行；网关对 `custom_openai` 自有模型会把这个工具直接丢掉
（实测响应里 `tools` 是空的）。Claude Code 自带的 `WebSearch` 同理。LiteLLM 自己的 Search Tools
（`/v1/search`）是另一套端点族，Codex 根本不会去调。

唯一的出路是**把搜索做成一个模型能主动调用的工具**，也就是 MCP。三个客户端
（Linux codex CLI、VSCode Codex 插件、VSCode Claude Code）都是 MCP 客户端，连同一个地址即可，
与它们背后是什么模型无关。

```
searxng   --proxy-->  proxy:7890          搜索引擎上游，和 OAuth 同一条代理链路
searxng-mcp --HTTP--> searxng:8080        JSON API（format=json）
litellm   --MCP-----> searxng-mcp:8090    网关代理它，三个客户端都从这条路径搜
客户端    --HTTPS--->  <网关>:4000/searxng/mcp
```

## ★ 为什么 MCP 要挂在 LiteLLM 后面（每用户一把 key）

`searxng-mcp` **不发布端口**，只在 compose 网络里可见；客户端不去连它，而是连
**网关的 `/searxng/mcp`**。这样做的直接结果是：

- 客户端用的就是它**本来就有的 LiteLLM virtual key**——用户手里只有一把 key，
  没有第二个东西要理解、要粘贴。搜索配置和模型配置在同一条命令里完成。
- `MCP_SEARXNG_TOKEN` 降级为**服务端内部凭据**：只存在于网关的注册记录里，用于
  「网关 → searxng-mcp」这一跳。用户永远看不到它，和 `CLIPROXY_API_KEY` 之于
  「litellm → cli-proxy-api」是同一个角色。
- 端口不对外，所以「能被谁调用」不再依赖那层 Host 白名单来兜底。

网关侧要一次性注册一次（**管理员动作，不是用户动作**）：

```bash
python searxng/register_mcp.py --dry-run   # 先看要发什么，token 打码
python searxng/register_mcp.py             # 注册/更新，幂等
python searxng/register_mcp.py --check     # 只看当前注册状态和健康
```

注册记录存在 Postgres（compose 里 `STORE_MODEL_IN_DB: True`），**重启不丢**，所以它
不该进 compose 的启动流程。改完 `MCP_SEARXNG_TOKEN` 重跑一次即可。

> ⚠️ **不要写「先 GET 注册记录、改几个字段、再 PUT 回去」的代码。**
> `GET /v1/mcp/server` **从不回显 `credentials`**（永远是 `null`——密钥不回传是对的），
> 读-改-写会把存着的 token 一起抹掉。症状很隐蔽：改动看起来生效了，但网关连不上
> `searxng-mcp`，`tools/list` 全空，而 `/health` 从 `healthy` 变成 `unknown`。
> PUT 必须发**完整**请求体，凭据从 `.env` 重新取。`register_mcp.py` 就是这么做的。

## 文件与变量

| 位置 | 作用 |
|---|---|
| `searxng/settings.yml` | 挂进容器 `/etc/searxng/settings.yml`，**可写**，见下方「为什么可写」 |
| `searxng/register_mcp.py` | 把本服务注册进 LiteLLM 网关。幂等，服务器上跑一次 |
| `searxng/README.md` | 本文件：部署与分层验证 |
| `docker-compose.yml` | `searxng`、`searxng-mcp` 两个服务（`litellm` 依赖后者） |
| `.env` | `SEARXNG_SECRET`、`MCP_SEARXNG_TOKEN`、两个白名单、两个可选镜像覆盖 |
| 客户端配置 | 由 `private-api/search.py` 写入，两个工具包各一份；**由主流程自动完成** |

`.env` 里这几个变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `MCP_SEARXNG_TOKEN` | **是** | **服务端内部凭据**，只给「网关 → searxng-mcp」这一跳用，客户端不再需要它。`.env` 里不设它，`docker compose up` 会直接报错退出 |
| `SEARXNG_SECRET` | **是** | SearXNG 的 `secret_key`。不设会退化成 `ultrasecretkey` 并在日志里告警 |
| `MCP_SEARXNG_ALLOWED_HOSTS` | 是 | `searxng-mcp:8090`。**用服务名，不要用 IP**，见下面第 1 个坑 |
| `MCP_SEARXNG_ALLOWED_ORIGINS` | 是 | `http://searxng-mcp:8090`。非浏览器客户端不带 Origin，这一项只是满足「加固模式必须显式配置」 |
| `SEARXNG_IMAGE` | 否 | 固定 SearXNG 镜像版本，默认 `searxng/searxng:latest` |
| `MCP_SEARXNG_IMAGE` | 否 | 固定 MCP 镜像版本，默认 `isokoliuk/mcp-searxng:latest` |

> 两个白名单项在 compose 里也有**同形的默认值**，所以不设 `.env` 也能起来；显式写进
> `.env` 是为了让「这个部署用的是哪个地址」一眼可见。`register_mcp.py` 每次运行都会
> 核对这两处和它要注册的 URL 是否同源，对不上会往 stderr 报警——它只核对，不自动推导。

### 为什么 `settings.yml` 是**可写**挂载

镜像的 entrypoint 会 grep `secret_key` 是不是还是 `ultrasecretkey` 这个默认字面量，如果是就用
`SEARXNG_SECRET` 替换。只读挂载会让这一步失败。所以不要给它加 `:ro`。

同样地：`searxng` 镜像的 entrypoint **只在 `/etc/searxng/settings.yml` 不存在时**才用环境变量生成
配置。我们挂了这份文件，所以**环境变量对它无效，一切以 `settings.yml` 为准**。

### ⚠️ `settings.yml` 里不能写 `!ENV`，也不能引用任何环境变量

SearXNG 是拿**不带自定义 constructor 的 `yaml.safe_load`** 读这个文件的。写 `!ENV [VAR, "默认值"]`
不会「从环境变量取值」，而是让容器在 `init_settings()` 阶段直接崩掉：

```
yaml.constructor.ConstructorError: could not determine a constructor for the tag '!ENV'
  in "/etc/searxng/settings.yml", line 26, column 15
searx.exceptions.SearxSettingsException: could not determine a constructor for the tag '!ENV'
```

症状很有迷惑性：`docker compose ps` 显示 `Restarting (1)`、`docker inspect` 里
`RestartCount` 一路涨，而 `State.Health` 是 `{"Status":"unhealthy","FailingStreak":0,"Log":[]}`
——**`FailingStreak: 0` 和空的 `Log` 说明 healthcheck 根本没跑过**，是应用先死的。看到这个组合
就直接去看 `docker compose logs searxng`，不要浪费时间调 healthcheck 命令。

（2026.9.17 的镜像实测如此。所以本文件里的代理地址是**硬编码**的，改代理直接改
`outgoing.proxies` 那两行。）

**改完 `settings.yml` 要 `docker compose restart searxng`** —— 容器启动时才读这个文件。

## 启动

```bash
# 1) 确认 .env 里 MCP_SEARXNG_TOKEN 和 SEARXNG_SECRET 都已设置
grep -c '^MCP_SEARXNG_TOKEN=' .env   # 应为 1

# 2) 起这两个服务（proxy 会被自动带上）
docker compose up -d searxng searxng-mcp

# 3) 看状态。searxng-mcp 会等 searxng 的 healthcheck 通过才起
docker compose ps searxng searxng-mcp

# 4) ★ 确认 secret_key 真的被换掉了，以及它换到哪儿去了（见下一节）
docker compose exec searxng grep 'secret_key' /etc/searxng/settings.yml
git status --short searxng/settings.yml

# 5) ★ 注册进网关，并确认网关→searxng-mcp 这一跳真的通
python searxng/register_mcp.py --check
#   url 应为 http://searxng-mcp:8090/mcp，且 status 是 healthy
```

**第 5 步的 `healthy` 是这整套东西的关键闸门。** 它证明网关能连上 `searxng-mcp`——
也就是 Host 白名单、token、网络都对了。只有它能过，客户端才可能有搜索。

### 从旧版本切过来（原来客户端直连 8090）

旧布局里客户端直连 `10.18.219.156:8090`，网关的注册记录也是那个地址。切换要点是
**先让新 Host 可用，再切注册，最后才收端口**，这样搜索全程不会断：

```bash
# 1) .env 里两个 Host 都收；此时旧注册照常工作
MCP_SEARXNG_ALLOWED_HOSTS=searxng-mcp:8090,10.18.219.156:8090
docker compose up -d searxng-mcp

# 2) 把注册切到内网名
python searxng/register_mcp.py && python searxng/register_mcp.py --check

# 3) 第 2 步绿了之后，把 .env 那行改回只有 searxng-mcp:8090，
#    端口映射已经在 compose 里改成 expose 了
docker compose up -d searxng-mcp
```

任何一步不绿就**停在原地**，旧路径仍然可用。

### 客户端侧不用手动做任何事

搜索配置已经折进主流程的最后一步，并且**复用的是这次刚写进去的 LiteLLM key**。
所以只有一条命令（见 `vscode/README.md` / `codexcli/README.md`）：

```bash
python private_api.py --target both --api-base http://<网关>:4000 --api-key sk-你自己的
```

`--no-search` 可以跳过这一步；`--configure-search` / `--check-search` / `--search-clear`
仍然保留，用于单独重配、排查和撤销。

### ★ 先提交，再启动 —— 否则密钥可能悄悄进了 git

`searxng/settings.yml` 现在**还没有被 git 跟踪**（`searxng/` 整个目录都是新增的），而
`SEARXNG_SECRET` 是真实密钥。这两件事合在一起有个顺序陷阱：

> 如果 entrypoint 是**就地改写宿主机上这个挂载文件**的，那么「先 `docker compose up`、
> 后 `git add searxng/`」就会把换成真实密钥的那一版提交上去。`.gitignore` **拦不住**已经
> 写进文件、等着被 `git add` 的内容。

所以顺序要反过来 —— **先用 `ultrasecretkey` 这个占位值提交，再启动容器**：

```bash
git add searxng/ && git commit -m "add searxng"      # 此时 secret_key 还是占位值
docker compose up -d searxng searxng-mcp
```

提交之后再启动，entrypoint 无论怎么改，都会以**一个可见的 diff** 的形式暴露出来，而不是
被首次 `git add` 囫囵吞进去。之后每次启动都按下面这步检查。

### 启动后检查（第 4 步的展开）

- `docker compose exec searxng grep secret_key /etc/searxng/settings.yml` 应该显示 **`.env` 里
  那个真实值**，不是 `ultrasecretkey`。还是占位值说明替换没成功（`SEARXNG_SECRET` 没设，
  或者挂载被加了 `:ro`），去 `docker compose logs searxng` 找告警。
- `git status --short searxng/settings.yml` **必须是空输出**。显示 ` M` 就说明 entrypoint 是
  **就地改写**宿主机这个文件的，真实密钥已经躺在工作区里了 —— `git add -A` 会把它提交上去。

第二种情况下，立刻还原，然后二选一：

```bash
git checkout -- searxng/settings.yml      # 先还原
```

```bash
# 方案 A（根治）：别让容器碰仓库里的文件 —— 挂成模板，让 entrypoint 改容器内的副本
#   docker-compose.yml 的 searxng.volumes 改成：
#     - ./searxng/settings.yml:/etc/searxng/settings.yml:ro
#   再加一个 tmpfs 或具名卷盖住 /etc/searxng，并把文件 cp 进去。
```

```bash
# 方案 B（权宜）：接受文件被改写，但从提交里排除它的后续改动
git update-index --skip-worktree searxng/settings.yml
```

方案 B 能挡住 `git add`，但代价是：**这个文件从此和仓库脱钩**，别人拉下来拿到的仍是
`ultrasecretkey`（这是对的），而你自己也再收不到它的上游更新。方案 A 才是根治。

> **作者的坦白：** 我**没有** Docker，无法验证这个镜像的 entrypoint 到底是「就地改写挂载文件」
> 还是「写去容器内另一个路径」。上面两条命令是**必须由你执行的判定依据**，不是我替你下过的结论。
> 在它跑出结果之前，本文件（以及 `docker-compose.yml` 里对应的注释）都只是**标注了这个不确定性**，
> 不是断言。

## 分层验证 —— 从里往外，五层

**这五层必须按顺序做。** 每一层失败都比下一层更容易定位；跳过前几层直接在客户端点搜索，
只会看到一个笼统的失败。

### 第 1 层：SearXNG 自己能不能搜

`searxng` 不发布端口，所以要进容器里问：

```bash
docker compose exec searxng wget -qO- 'http://127.0.0.1:8080/search?q=test&format=json' | head -c 400
```

- 返回 JSON → 过。
- 返回 **403 或一页 HTML** → `settings.yml` 的 `search.formats` 里丢了 `json`。SearXNG 对
  `format=json` 的请求会用 403 + HTML 拒绝，而 MCP 服务会把它报成「Content-Type 不是 JSON」，
  这个报错指不到真正的原因。当前配置里 `html` 和 `json` 都在，别删。

顺便确认上游代理通不通（搜索引擎都在墙外，走的是 mihomo）：

```bash
docker compose logs --tail=30 searxng
```

出现大面积 `Timeout` / 连接错误，多半是 `proxy` 没起或者节点规则不对——和 CLIProxyAPI 出网是
同一条链路，去 `network/mihomo` 那边查。

### 第 2 层：MCP 服务的 `/health`

```bash
docker compose exec searxng-mcp node -e "require('http').get('http://127.0.0.1:8090/health',r=>{console.log(r.statusCode)})"
```

**这一层绿了什么都不代表。** `/health` 是官方明确说明的免鉴权端点，它也不会去碰 SearXNG。
它只说明 HTTP 还活着。这正是 compose 里 healthcheck 只探它的原因——把真搜索接进 healthcheck
会让「SearXNG 挂了」变成 MCP 容器无限重启。

### 第 3 层：网关的注册与「网关 → searxng-mcp」这一跳

```bash
python searxng/register_mcp.py --check
```

要同时满足两件事：

- `url` 是 `http://searxng-mcp:8090/mcp`（内网服务名，不是 IP）
- `status` 是 `healthy`

`healthy` 才代表网关真的连上了后端。这一跳同时覆盖 Host 白名单、内部 token 和网络，
所以**它绿了，第 1、2 层其实也就不用再怀疑了**。

> ⚠️ 一个很坑的失败形态：`/v1/mcp/server/health` 报 `healthy` 只说明「网关能连上」。
> 如果给它的 Host 不在 `MCP_HTTP_ALLOWED_HOSTS` 里，它会报 `unhealthy`/`unknown`——
> 而 `searxng-mcp` 自己的 `/health`（第 2 层）**照样是绿的**。这也是为什么这两层要分开看。

### 第 4 层：拿一个**普通用户的 key** 走网关做真实握手和搜索

这是唯一能证明「用户视角真的能用」的一步。**用普通用户的 virtual key，不要用 master key**
——`allow_all_keys: true` 就是为这件事设的，用 master key 验等于没验。

```bash
USER_KEY='sk-某个普通用户的key'

# 4a) initialize，记下响应头里的 Mcp-Session-Id
curl -sS -D- -o /tmp/mcp-init.json -X POST http://<网关>:4000/searxng/mcp \
  -H "Authorization: Bearer $USER_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

> `Accept` 必须**同时**包含 `application/json` 和 `text/event-stream`：MCP 的 streamable-HTTP
> 规范允许服务端对同一个请求自由选择用哪种格式回，只写一种会被拒。

```bash
# 4b) 把会话 id 填进去
SID='<上一步响应头里的 Mcp-Session-Id>'

# 4c) 列出工具。响应可能是 JSON，也可能是 SSE 帧（data: {...} 一行）
curl -sS -X POST http://<网关>:4000/searxng/mcp \
  -H "Authorization: Bearer $USER_KEY" -H 'Mcp-Session-Id: '"$SID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | head -c 1200
```

> ★ **预期看到的工具名带 `searxng-` 前缀**：`searxng-searxng_web_search`、
> `searxng-web_url_read`、`searxng-searxng_instance_info`、`searxng-searxng_search_suggestions`。
> 前缀是网关加的，**改不掉**——实测把 `tool_name_to_display_name` 设成完整反向映射后，
> `tools/list` 返回的**仍然是**前缀名，那个字段只影响显示层。所以任何按工具名做校验的代码
> 都必须两种拼法都认（客户端侧已经用 `normalize_tool` 处理了）。

每个工具的 `inputSchema` 里写着它的参数名——**照它填**，不要凭印象猜（`query` 还是 `q`，
以这个输出为准）。

```bash
# 4d) 真的读一次正文（要出网，走 proxy）。这里用带前缀的名字。
curl -sS -X POST http://<网关>:4000/searxng/mcp \
  -H "Authorization: Bearer $USER_KEY" -H 'Mcp-Session-Id: '"$SID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"searxng-web_url_read","arguments":{"url":"https://example.com"}}}' | head -c 800
```

```bash
# 4e) 不带 key 应当被拒 —— 证明 key 是唯一的闸门
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://<网关>:4000/searxng/mcp \
  -H 'Content-Type: application/json' -d '{}'
```

### 第 5 层：客户端侧

客户端配置由主流程自动写入（见 `codexcli/README.md` 和 `vscode/README.md` 的搜索一节）。
从**客户端机器**跑：

```bash
python private_api.py --check-search
```

它会用**配置里真正会发送的那个 token**（不是当前 shell 的环境变量——两者不一致正是要诊断的故障）
走一遍第 4 层的握手，并把 401/403/404 翻译成人话。它还会报出「配置里的 URL」和「当前网关推导出的
端点」是否已经漂移——旧配置在网关换地址之后不会自己更新，症状是连接错误而不是配置错误。

## 三个坑，按踩到的概率排序

### 1. Host 白名单 —— `healthy` 变 `unhealthy`，而 searxng-mcp 的 `/health` 还是绿的

`MCP_HTTP_ALLOWED_HOSTS` 默认只有 `127.0.0.1` / `localhost` / `[::1]`，并且是拿它跟请求的
**`Host` 头连端口精确比对**的，不是子串匹配、也不是按来源 IP 过滤。网关以容器身份来连，
`Host` 就是 `searxng-mcp:8090`。

**用服务名，不要用 IP。** 服务名在任何环境下都一样，所以这一项不含环境相关的配置；
用 IP 就变成「换机器要记得改」，而漏改的症状是——网关那一跳报 `unhealthy`，而
`searxng-mcp` 自己的 `/health` 照样是绿的，指错方向。

`register_mcp.py` 每次跑都会核对 `.env`（或 compose 默认值）里这一项和它注册的 URL
是否同源，对不上会报警。**它只核对，不自动推导**——推导要么引入 yaml 依赖、要么起
subprocess、要么写脆弱正则，每种都自带新的失败模式；而危害本来就不是「不一致」，
是「不一致之后静默」。

### 2. `401 Unauthorized`（客户端侧）

客户端现在用的是**你自己的 LiteLLM key**。所以 401 基本只有一个原因：搜索那一步跑在
模型配置之前，或者用了和模型配置不同的 `--api-key`。**不存在第二个搜索 key 了。**

（如果 `MCP_SEARXNG_TOKEN` 和 `searxng-mcp` 对不上，症状是网关那跳报 `unhealthy`，
而不是客户端 401——因为客户端根本不碰那个 token。）

### 3. 404 `MCP server ... not found` —— 没注册

网关起来了但不知道 `searxng` 这个 MCP server。在**网关那台机器**上跑
`python searxng/register_mcp.py`。未注册时网关的原话是
`MCP server, toolset, or access group 'searxng' not found`。

### 4. `/health` 绿，但一搜就失败

回到第 1 层。MCP 服务只是转发，SearXNG 搜不出来（`json` 格式被禁、上游代理不通、引擎被限流）
它都无能为力。

## 四个工具，以及为什么用上游镜像

服务端用的是开源镜像 `isokoliuk/mcp-searxng`，不是自己写的适配器。选它的原因**不是鉴权，是能力**：

| 工具 | 作用 |
|---|---|
| `searxng_web_search` | 搜索，支持分页、过滤、direct answers |
| `web_url_read` | **把指定 URL 读成 text/Markdown**，可提取标题层级、指定章节、有界 PDF 正文 |
| `searxng_instance_info` | 查实例启用了哪些 categories / engines，模型用错分类名时能自查 |
| `searxng_search_suggestions` | 查询补全 / 改写 |

**`web_url_read` 是分水岭。** 只有搜索的话，模型拿到一堆标题和链接就没辙了；而「搜到 → 读几篇 →
综合」恰恰是研究类任务的主路径。缺了「读」这半边，搜索的价值大打折扣。

它另外还自带多实例故障转移、JSON 失败回退 HTML、缓存、限流与并发上限、Cosign 签名镜像。

代价说清楚：镜像里带 Node 22 运行时，体积和受攻击面都比一个纯标准库脚本大；配置面也更大
（第 1 个坑就是它的默认值带来的）。

## 换机器 / 换地址的清单

**好消息是这份清单现在几乎是空的。** 客户端连的是**网关地址**（它们本来就配了），
网关连后端用的是**compose 服务名**，两边都不含环境相关的常量。所以：

| 动了什么 | 要改什么 |
|---|---|
| 换机器 / 换 IP / 换网关地址 | **服务端什么都不用改。** 客户端本来就要传 `--api-base` |
| 换 `searxng-mcp` 的**服务名**或**端口** | `.env` 的 `MCP_SEARXNG_ALLOWED_HOSTS` + `register_mcp.py` 里的 `INTERNAL_HOSTPORT`（脚本会核对两者，不一致会报警） |
| 换了 `MCP_SEARXNG_TOKEN` | 重跑 `register_mcp.py`。**客户端不用动** |
| 客户端配置指向了旧地址 | 重跑主流程命令，它会重写；`--check-search` 会报出漂移 |

> 曾经的清单里第一条是「`.env` 里改 `MCP_SEARXNG_ALLOWED_HOSTS` 的 IP，漏了就全 403」。
> 那条之所以存在，是因为客户端直连 8090、`Host` 头里带的是宿主 IP。改成经网关之后，
> 那一类配置连同「记得改 IP」这件事一起消失了。

## 哪些已经实测过，哪些还没有

**已经在线实测（2026-09-17，LiteLLM `v1.100.1`）：**

- SearXNG 能起、能搜；`searxng-mcp` 能起。
- 网关的 MCP 代理**能跑通完整三跳**：`initialize` → `tools/list` → `tools/call`，
  SSE 格式，返回 `mcp-session-id`。`web_url_read` 抓回的正文是真的网页内容。
- **工具名会被加 `searxng-` 前缀**（见第 4 层），且 `tool_name_to_display_name` 改不掉它。
- **`allow_all_keys: true` 确实生效**：一把临时生成的**普通 virtual key**（不是 master key）
  就能完成握手和真实工具调用 → 「每用户一把 key」这个设计成立。
- `Host` 白名单是**连端口精确比对**：同一个来源 IP、同一个目标，只改 `Host` 头，
  `searxng-mcp:8090` 与 IP 形式一个 200 一个 403。
- `GET /v1/mcp/server` 不回显 `credentials`；`PUT` 返回 **202**；`PUT` 不带 `server_id` 是 **422**。

**还没验证的：**

- **VSCode 里 Claude Code 的 MCP 客户端**。这台机器上没有 `claude` CLI，扩展只发布了
  `extension.js`，所以「配置写对了」是用代码和文档推出来的，**没有在真实进程里跑过**。
  Codex 那半边是真跑过的（真实会话里发出过 `mcp_tool_call` 并拿到搜索结果）。
  → Claude Code 那边请务必自己点一次搜索确认。
- 客户端 `search.py` 的写盘行为有 48 项桩测试（TOML/JSONC 往返、注释保留、幂等、
  401/403/404 诊断、JSON 与 SSE 两种响应形态），这些和 MCP 服务无关，仍然成立。

> 写这段时踩过一个值得记的坑：第一轮校验 `settings.yml` 时我给 `!ENV` 开了绿灯——我以为那是
> SearXNG 自己的标签。但我是拿一个**装了自定义 constructor 的加载器**去验的，所以它当然能过；
> 而 SearXNG 用的是**裸 `safe_load`**，在真实容器上直接崩了。教训是：验证「某个工具能不能读
> 这个文件」时，必须用**那个工具自己的加载路径**，不能用我手边顺手的等价物。
> 后来同样性质的错误又犯了一次：我用「GET 注册记录再 PUT 回去」去测一个字段，结果把线上
> 存着的 token 抹掉了——**验证手法本身改变了被验证的东西**，也是同一类错误。
