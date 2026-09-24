# LiteLLM + CLIProxyAPI + mihomo 部署说明

本工程把 LiteLLM、CLIProxyAPI、mihomo 和 PostgreSQL 全部放进同一套 Docker Compose。目标链路是：

```text
浏览器/客户端
    │
    ├── :8787  proxy-console（节点切换 + Codex OAuth 登录控制面）
    │                 │
    │                 ├── cli-proxy-api:8317/v0/management
    │                 └── proxy:9090
    │
    └── :4000  LiteLLM ──OpenAI API──> cli-proxy-api:8317
                                      │
                                      └── proxy:7890 ──> ChatGPT/Codex 上游
```

`proxy` 和 `proxy-console` 是两个独立容器：前者是 mihomo 数据面，真正转发流量；后者只是网页控制面，不转发业务流量。`cli-proxy-api` 同时提供 OpenAI 兼容模型接口和 OAuth 管理接口；LiteLLM 只调用它的模型接口，网页登录只通过 `proxy-console` 完成。

## 服务与端口

| 服务 | 作用 | 对外端口 |
| --- | --- | --- |
| `proxy` | 运行仓库内的 mihomo 二进制和 `mihomo/config.yaml` | 无；`7890`/`9090` 仅 Compose 网络 |
| `cli-proxy-api` | CLIProxyAPI OpenAI 兼容接口、Codex OAuth、账号路由 | 无；`8317` 仅 Compose 网络 |
| `proxy-console` | 节点筛选/切换、订阅刷新、Codex 登录 | `8787` |
| `litellm` | 统一 API 网关和模型配置 | `4000` |
| `db` | LiteLLM PostgreSQL 数据库 | 无；`5432` 仅 Compose 网络 |

CLIProxyAPI 使用官方镜像 `eceasy/cli-proxy-api:v7.2.157`，Compose 启动时自动拉取；不需要你下载或提交 GitHub 源码。源码仅在需要自行编译、调试上游时才有必要。上游项目和配置说明见 [CLIProxyAPI GitHub](https://github.com/router-for-me/CLIProxyAPI)。

## 首次配置

不要把密钥写进 `docker-compose.yml`。在项目根目录 `.env` 中保留现有 LiteLLM/PostgreSQL/控制台变量，并新增两个彼此不同的随机值：

```dotenv
# CLIProxyAPI 数据面：LiteLLM 调用 http://cli-proxy-api:8317/v1 时使用
CLIPROXY_API_KEY=替换为随机长字符串

# CLIProxyAPI 管理面：只由 proxy-console 使用，不能和上面相同
CLIPROXY_MANAGEMENT_KEY=替换为另一组随机长字符串
```

两把钥匙用途不同：`CLIPROXY_API_KEY` 会出现在模型请求的 `Authorization: Bearer ...` 中；`CLIPROXY_MANAGEMENT_KEY` 只用于 `/v0/management`，控制台不会把它返回到浏览器。不要把它们填成 LiteLLM 的 `LITELLM_MASTER_KEY`。

Compose 会把 `cliproxyapi/config.yaml.template` 渲染成容器内的配置，CLIProxyAPI 的 OAuth 文件持久化到命名卷 `cliproxy_auths`，因此重建容器不会要求重新登录。配置模板里的 `proxy-url: http://proxy:7890` 让 CLIProxyAPI 通过 mihomo 出网。

## 启动

```bash
cd <litellm-0914-deploy>
docker compose pull
docker compose up -d
docker compose ps
```

七个服务应当都处于运行状态（`proxy`、`cli-proxy-api`、`proxy-console`、`litellm`、`db`、`searxng`、`searxng-mcp`）。`proxy` 的 healthcheck 在不同 Alpine/busybox 版本上可能显示 `unhealthy`，这只影响状态标记，不影响 `service_started` 依赖下的实际转发；详见“常见问题”。

## 在控制台登录 Codex

1. 浏览器打开 `http://<服务器IP>:8787`，用 `.env` 的 `CONSOLE_USER` / `CONSOLE_PASSWORD` 登录。
2. 在“ChatGPT/Codex 订阅”面板点击“登录 Codex”。控制台向 CLIProxyAPI 请求一次性 OAuth URL，并自动打开新窗口。
3. 在 OAuth 页面完成 ChatGPT 登录和授权。若回调能直接回到 CLIProxyAPI，页面会自动轮询并显示“已登录”。
4. 如果远程服务器环境导致回调地址落到浏览器的 `localhost:1455`，不要把 1455 暴露到公网：复制浏览器地址栏中的完整回调 URL，粘贴到控制台的“OAuth 回调地址”输入框并提交。控制台会把它转发给 CLIProxyAPI 的 `/v0/management/oauth-callback`。
5. “刷新账号/用量快照”可以查看 CLIProxyAPI 已保存的非敏感账号信息。OAuth 凭据实际保存在 `cliproxy_auths` 卷内。

账号面板的主配额百分比来自 `/auth-files` 的
`quota.signals.X-Codex-Primary-Used-Percent`：**73% 是已用，剩余 27%**，
不是整个订阅周期的总用量。未返回有效百分比时显示“用量未知”。
页面可见且未进行 OAuth 登录/自检时，每 30 秒读取一次快照，也可手动刷新；
自检完成后会重新读取账号信息。这里只读管理面记录，不主动查询上游额度，
不额外发送生成请求；“快照读取时间”不是上游用量采样时间。

账号面板的 `status/status_message` 与下方自检是两套信息：
前者是 CLIProxyAPI 管理面的账号状态记录，后者是这一次请求的结果。
旧页面生成后没有刷新账号面板，可能出现上方仍显示旧错误、下方已成功的情况。
新版会刷新，但如果管理面仍报告异常，会保留告警并标明“非本次自检结果”；
不会因为某个模型的一次调用成功，就将所有账号/模型标记为健康。

如果浏览器阻止了弹窗，把按钮返回的登录地址复制到新标签页打开即可；登录状态仍由控制台轮询。

### 确认登录真的生效

网页显示"已登录"只说明 OAuth 回调走完了，**不说明凭据能用**。账号可能已过期、节点可能挂了、账号可能没额度 —— 这些都要实际发一个请求才看得出来。同一个面板下有两个按钮：

- **连通性自检** —— 调数据面 `GET /v1/models`（证明 CLIProxyAPI 活着、数据面 key 对、它认识哪些模型名）和管理面 `/auth-files`（证明有凭据文件）。**这一步通过不代表能用。**
- **实际生成一次** —— 通过 `/v1/chat/completions` 发一个 `ping`（`max_tokens: 8`）。这才是"真的能用"的证据。模型名留空时自动挑一个带 `codex` 的；自检会把挑中的模型写回输入框，可以针对某个模型重测。

结果按步骤显示，每一步都把上游返回的**原文**贴出来 —— 排障时那句原始报错比任何猜测都有用。几种常见组合：

| 现象 | 说明 |
| --- | --- |
| 第一步 401 | `CLIPROXY_API_KEY` 和 CLIProxyAPI 的 `api-keys` 不一致 |
| 第一步通、第二步说"没有已保存的账号" | 登录没真正落到 `cliproxy_auths` 卷里，重新登录 |
| 一档都通过、二档报上游错误 | 凭据或网络的问题，看原文：多半是 token 过期、节点不通或账号没额度 |
| 二档报不支持 `chat/completions` | 该模型只提供 Responses API，换一个模型测 |

自检走的是容器内部地址，所以它验证的是"**控制台 → CLIProxyAPI**"这一段。CLIProxyAPI 往上游走的那一跳（`proxy-url` → mihomo → 节点）由二档的实际生成间接证明：生成成功就说明那一跳是通的，失败则原文里会带上游的报错。想在命令行里跑同样的检查（控制台容器里两个环境变量都有）：

```bash
docker compose exec proxy-console python3 -c "
import json, os, urllib.request
req = urllib.request.Request(os.environ['CLIPROXY_BASE_URL'] + '/models',
    headers={'Authorization': 'Bearer ' + os.environ['CLIPROXY_API_KEY']})
print(json.dumps(json.load(urllib.request.urlopen(req, timeout=15)), ensure_ascii=False, indent=2))
"
```

## 在 LiteLLM 中接入 CLIProxyAPI

先确认 CLIProxyAPI 已有 Codex 账号，再在 LiteLLM 管理界面（`http://<服务器IP>:4000`）新增一个 OpenAI 兼容模型：

| LiteLLM 字段 | 填写 |
| --- | --- |
| Provider | `openai` |
| API Base / Base URL | `http://cli-proxy-api:8317/v1` |
| API Key | `.env` 中的 `CLIPROXY_API_KEY` |
| Model | 选择 CLIProxyAPI `/v1/models` 返回的 Codex 模型名，不要臆填版本号 |

LiteLLM 容器内可以直接检查模型列表（命令只在 Compose 网络内执行）：

```bash
docker compose exec litellm python -c "import httpx; r=httpx.get('http://cli-proxy-api:8317/v1/models', headers={'Authorization':'Bearer <CLIPROXY_API_KEY>'}, timeout=15); print(r.status_code); print(r.text[:1000])"
```

之后客户端只需要调用 LiteLLM 的 `http://<服务器IP>:4000/v1`，不需要知道 CLIProxyAPI 或 mihomo 的端口。

LiteLLM 容器**不设** `HTTP_PROXY/HTTPS_PROXY`。原因是按模型分开代理这件事本来就不在 LiteLLM 这一层决定：

- 经过 CLIProxyAPI 的模型（不管有几个），LiteLLM 都只是调 `http://cli-proxy-api:8317/v1`，是同一个容器间地址。要不要翻墙取决于 **CLIProxyAPI 拿到的那个上游域名**，由 `proxy-url` 交给 mihomo、再由 mihomo 规则决定。
- 不经过 CLIProxyAPI 的自有 endpoint，目前没有翻墙需求，直连即可。

所以整个工程里"要不要走节点"只有一个决策点：**mihomo 规则**（见下一节）。`NO_PROXY` 仍然留在 compose 里，是为了以后某个自有 endpoint 需要走节点、把 `HTTP_PROXY` 取消注释时用的，那份清单已经备好。

需要注意：某个上游域名若要走直连，mihomo 得能自己解析它（用 `direct-nameserver`）。如果某个"不需要代理"的上游只在别的容器 `/etc/hosts` 里可解析，mihomo 会解析失败 —— 那种域名得放进该容器的 `NO_PROXY`，不能只加直连规则。

## 配置代理例外（哪些网址不走代理）

链路上有四个地方能决定「走不走代理」，但真正起决定作用的只有 mihomo：

| 层次 | 作用范围 | 谁能改 |
| --- | --- | --- |
| `mihomo` 规则 | 全部经过 mihomo 的流量 | 控制台网页，或手工改 `.list` |
| CLIProxyAPI 的 `proxy-url` | CLIProxyAPI 所有出网请求，**全局生效、没有按域名绕过的开关** | 只能靠 mihomo 规则 |
| 容器 `HTTP_PROXY` / `NO_PROXY` | 只看容器自己发起的请求，且只有认这两个变量的程序才认。**当前没有容器设置它**（LiteLLM 已不设，理由见上一节） | `.env` / compose，改完要重启容器 |
| 宿主机 / docker daemon 代理 | `docker compose pull` 等宿主机命令 | 系统 systemd 配置 |

所以例外清单放在 mihomo 这一层：经 CLIProxyAPI 出去的请求全部经过 mihomo，规则都算数。**按模型分开代理，最终就是按上游域名分开** —— 上游域名不同的模型可以分到不同的清单里，上游域名相同的模型则区分不了（mihomo 只看域名，不看账号）。控制台的「代理例外」面板读写的就是 mihomo 的两个本地规则集。

两个清单的区别：

- **直连例外（`CustomDirect.list`）** —— 命中就走真实 IP 直出，不经过任何节点。内网域名、必须直连的域名放这里。
- **强制走代理（`CustomProxy.list`）** —— 命中就强制从节点出去。用于纠正被 `ChinaDomain` / `GEOIP,CN` 误判成国内地址、实际需要翻墙的域名。它的出口是控制台上的「🚀 节点选择」分组，那个分组如果被切成 DIRECT，这里的域名照样直连。

在网页上操作：选清单、选匹配方式（完整域名 / 域名后缀 / 域名关键字 / IPv4 网段 / IPv6 网段 / GEOIP）、填值、点「添加并生效」。改动会写进 `network/mihomo/ruleset/*.list`，然后让 mihomo 重新读取，**不需要重启容器**。匹配方式选好后值会被规范化：粘贴整条 URL、带端口、带 `*` 前缀都会自动只取域名；IP 段自动补 `no-resolve`。含空格、逗号、换行的输入会被拒绝 —— 这类字符会让整个规则集解析失败。

### 这一层不够用的时候

有些域名只有某个容器的 `/etc/hosts` / `extra_hosts` 里有解析（本项目里的 `aix-backup.hismarttv.com` 就是，它指向宿主机）。把请求交给 mihomo 反而解析不出来，所以这类域名必须让容器自己直连：控制台面板底部的 `NO_PROXY` 输入框已经把它们拼好了，复制出来覆盖 `docker-compose.yml` 里 `litellm` 的 `NO_PROXY`，然后 `docker compose up -d litellm`。

改完 `.list` 文件后如果 mihomo 没重新读取，面板会显示「已加载 N 条」对不上；点面板上的「重新应用」即可。`CustomDirect` / `CustomProxy` 这两个 rule-provider 必须在 `network/mihomo/config.yaml` 里声明过，且两个规则集要排在 `rules:` 最前面 —— mihomo 是首个匹配的规则生效，排到后面会被 `ProxyLite` / `ChinaDomain` / `MATCH` 抢先命中，网页上看起来配好了却没有效果。

## 部署后验证

验证 mihomo 能否出网。注意这里**必须显式指定代理**：LiteLLM 容器不再设代理环境变量，所以不能拿它当探针，改用 proxy-console 容器（自带 python，且和 proxy 在同一网络里）。`401` 说明已到达 OpenAI，只是没带 API key：

```bash
docker compose exec proxy-console python3 -c "
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler(
    {'http': 'http://proxy:7890', 'https': 'http://proxy:7890'}))
print(opener.open('https://api.openai.com/v1/models', timeout=20).status)
"
```

验证某个上游域名当前被判定成直连还是走节点，可以问 mihomo 的 DNS。配置里开了 `respect-rules: true` 和 `enhanced-mode: fake-ip`，所以走节点的域名会拿到 fake-ip（`198.18.x.x`），直连的域名会拿到真实 IP：

```bash
docker compose exec proxy-console python3 -c "
import json, urllib.request
url = 'http://proxy:9090/dns/query?name=你的上游域名&type=A'
print(json.dumps(json.load(urllib.request.urlopen(url, timeout=10)), ensure_ascii=False))
"
```

拿到 `198.18.x.x` 说明走节点，拿到真实 IP 说明直连。这是判断走哪一侧的近似手段（DNS 判定和规则判定在 `respect-rules` 下一致，但最终以实际请求为准）：接口通不通，仍以 CLIProxyAPI 日志和 `/v1/models` 为准。

验证内网地址没有被代理影响：

```bash
docker compose exec litellm python -c "import httpx; print(httpx.get('http://aix-backup.hismarttv.com/', timeout=15).status_code)"
```

查看关键日志：

```bash
docker compose logs -f cli-proxy-api proxy proxy-console litellm
```

## 配置文件和持久化

```text
docker-compose.yml                 七个服务和内部网络
cliproxyapi/config.yaml.template   CLIProxyAPI 基础配置（不含真实密钥）
cliproxyapi/entrypoint.sh          启动时渲染 CLIPROXY_API_KEY
network/proxy-console.py           节点控制台 + Codex OAuth 控制面 + 代理例外面板 + CLIProxyAPI 自检
network/mihomo/config.yaml         mihomo 节点订阅、规则、例外规则集声明和 external-controller
network/mihomo/ruleset/CustomDirect.list   直连例外清单（控制台读写）
network/mihomo/ruleset/CustomProxy.list    强制走代理清单（控制台读写）
network/mihomo-linux-amd64         mihomo 二进制
```

命名卷：

- `cliproxy_auths`：CLIProxyAPI OAuth 凭据，必须备份；不要提交到 Git。
- `cliproxy_logs` / `cliproxy_plugins`：CLIProxyAPI 日志和插件目录。
- `postgres_data`：LiteLLM 数据库。

查看卷名：`docker volume ls`。除非确认已经备份，否则不要使用 `docker compose down -v`，它会删除这些卷。

## PostgreSQL 16 与 18.4

当前 Compose 固定 `postgres:16`，这是 LiteLLM 官方部署通常采用的稳定大版本。PostgreSQL 大版本的数据目录不能直接在 18.4 和 16 之间复用：如果某个 `postgres_data` 卷已经由 18.4 初始化，不要直接改镜像后启动 16。应先用 18.4 做 `pg_dump`，再让 16 初始化新卷并用 `psql` 恢复；没有旧卷数据时直接用 16 启动即可。不要把 `docker compose down -v` 当成迁移命令。

## 常见问题

### `cli-proxy-api` 启动后立即退出

检查 `.env` 是否同时存在 `CLIPROXY_API_KEY` 和 `CLIPROXY_MANAGEMENT_KEY`，并确认两者非空：

```bash
docker compose config
docker compose logs --tail=100 cli-proxy-api
```

### 控制台显示 CLIProxyAPI 不可用

```bash
docker compose logs --tail=100 cli-proxy-api proxy-console
docker compose exec proxy-console python3 -c "import urllib.request,os; print(urllib.request.urlopen(os.environ['CLIPROXY_MANAGEMENT_URL'] + '/auth-files', headers={'Authorization':'Bearer ' + os.environ['CLIPROXY_MANAGEMENT_KEY']}, timeout=5).status)"
```

若返回 401，说明两边的 `CLIPROXY_MANAGEMENT_KEY` 不一致；若连接失败，检查服务名是否仍是 `cli-proxy-api` 以及容器是否在运行。

### Codex 登录停在“等待 OAuth 回调”

远程 Docker 环境最常见原因是回调地址指向用户电脑的 `localhost`，而不是服务器。复制浏览器地址栏完整 URL，粘贴到控制台输入框；不要把 CLIProxyAPI 的管理端口或 1455 回调端口映射到公网。也可以查看：

```bash
docker compose logs -f cli-proxy-api proxy-console
```

### 网页上加了例外，但流量还是走代理

按顺序查：

1. 面板上「直连例外」标题旁的标记是「未加载」还是「已加载 N 条」。「未加载」说明 mihomo 里没有这个 rule-provider：确认 `network/mihomo/config.yaml` 里声明了 `CustomDirect`，然后 `docker compose restart proxy`。
2. 标记显示「已加载 0 条」，但列表里明明有规则：点「重新应用」。若提示降级成了「整份重载配置」，当前 mihomo 版本没有单独刷新规则集的接口，配置重载会重置节点选择，请顺手确认「当前节点」。
3. 都已加载却仍然走代理：命中的是别的规则。两个例外规则集必须排在 `rules:` 最前面，检查它们有没有被上面新增的规则挤下去。
4. 域名只在某个容器的 `extra_hosts` 里有解析：这种情况 mihomo 解析不出来，必须把域名加进那个容器的 `NO_PROXY`（面板底部有现成的一行），再重启该容器。
5. 点「重新应用」时弹出 `503 {"message":"open /app/mihomo/ruleset/CustomDirect.list: no such file or directory"}`（`CustomProxy.list` 同理）：mihomo 读不到这个文件。它和面板写的**不是同一个路径** —— 面板写 `RULESET_DIR`（默认 `/ruleset`，挂的是 `./network/mihomo/ruleset`），mihomo 以 `/app/mihomo` 为工作目录、按 `path: ./ruleset/CustomDirect.list` 读 `/app/mihomo/ruleset/CustomDirect.list`。两者本来指向同一份宿主机目录，出现这个错误就说明**部署机上的 proxy 容器没有该文件**，或 `proxy` / `proxy-console` 不是同一份 compose 起的（目录挂载没生效）。修复：

   ```bash
   # 1. 对比两个容器看到的目录。正常情况两边应该是同一份文件（内容、时间戳一致）
   docker compose exec proxy-console ls -l /ruleset/
   docker compose exec proxy ls -l /app/mihomo/ruleset/

   # 2. 缺文件时，确认宿主机 network/mihomo/ruleset/ 下有这两个 .list（仓库自带），
   #    然后重建容器让挂载和自愈逻辑生效；restart 不会重新挂载目录。
   docker compose up -d --force-recreate proxy proxy-console

   # 3. 查 mihomo 日志里是否有 ruleset 相关报错
   docker compose logs --tail=100 proxy
   ```

   `docker-compose.yml` 里 proxy 的启动命令已经加了自愈：`mkdir -p /app/mihomo/ruleset`，两个 `.list` 不存在时创建空文件（不会覆盖已有内容）。控制台遇到 503 会降级成整份配置重载，并在重载后核对两个清单的条数；条数对不上会直接提示「两个容器看到的 ruleset 目录不是同一份」。

手工改过 `.list` 文件的话注意：控制台重写文件时会保留文件开头的注释块，**规则行之后的注释会被丢弃**，所以说明统一写在文件开头。

### `proxy` healthcheck 是 `unhealthy`

Alpine 的 busybox `wget` 参数和 GNU wget 不完全一致。确认 mihomo 日志没有退出后，可删除 `docker-compose.yml` 中 `proxy.healthcheck` 段，或按宿主机镜像版本调整参数；其它服务只依赖 `service_started`。

### mihomo TLS/x509 错误

容器现在把宿主机的 CA bundle 映射到容器内标准路径
`/etc/ssl/certs/ca-certificates.crt`。Debian/Ubuntu 默认通常使用：

```dotenv
# .env 可省略，默认值就是这个
HOST_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

如果宿主机是 RHEL/CentOS/AlmaLinux，先在宿主机确认 bundle 的真实路径：

```bash
readlink -f /etc/pki/tls/certs/ca-bundle.crt
```

然后在 `.env` 设置：

```dotenv
HOST_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt
```

修改 CA 路径后必须重新创建 `proxy` 容器：

```bash
docker compose up -d --force-recreate proxy
```

先分别验证宿主机和容器的 TLS：

```bash
# 宿主机：根路径没有订阅 token，返回 403 也可以；重点是不能出现证书校验错误
curl -I https://link.ssrsub.de/

# 容器：只验证 TLS 握手，不访问带 token 的真实订阅 URL
docker compose exec proxy \
  wget -S -O /dev/null https://link.ssrsub.de/
```

判断方式：

- 宿主机失败、容器也失败：服务器 CA、系统时间、出口 HTTPS 中间人或网络本身的问题；
- 宿主机成功、容器失败：容器 CA bundle 挂载或 `.env` 路径配置问题；
- 两边 TLS 都成功但真实订阅仍失败：再看订阅 URL、上游返回码和 mihomo 日志。

不要为了绕过这个错误设置 `skip-cert-verify: true`：如果服务器出口被 HTTPS
中间人代理替换证书，应把企业根 CA 正确加入宿主机 CA bundle，而不是关闭校验。

### 订阅刷新接口返回 404

这是 mihomo 版本差异。节点查看、筛选、切换仍可用；先看 `docker compose logs proxy-console`，必要时 `docker compose restart proxy` 触发启动时重新拉取订阅。

## 安全边界

- 只有 8787 和 4000 发布到宿主机；不要额外发布 7890、8317、9090。
- 8787 使用 Basic 鉴权；请设置强 `CONSOLE_PASSWORD`，并限制防火墙来源网段。
- 能登进 8787 就能改代理例外清单，也就是能决定哪些流量直连、哪些走节点。权限等同于改 mihomo 配置，所以 8787 不要暴露到公网。
- `CLIPROXY_MANAGEMENT_KEY` 权限高于模型 API key，只给控制台使用，不要发给客户端。
- `.env`、命名卷中的 OAuth JSON、mihomo 订阅缓存都可能含敏感信息，保持在 `.gitignore` 和备份访问控制范围内。
