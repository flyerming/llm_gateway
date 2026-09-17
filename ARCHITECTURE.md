# 方案设计说明

> 本工程把 **LiteLLM**、**CLIProxyAPI**、**mihomo**、**PostgreSQL** 收敛到一套 Docker Compose 里，
> 对外只暴露两个端口：`4000`（统一模型 API）和 `8787`（运维控制台）。
>
> 本文说明「为什么这么设计」和「各部件如何协作」。日常操作请看 [USAGE.md](USAGE.md)，
> 已知问题与改进建议请看 [REVIEW.md](REVIEW.md)。

---

## 1. 目标与约束

| 目标 | 说明 |
| --- | --- |
| 统一出口 | 客户端只认 `:4000/v1` 一个地址，兼容 OpenAI 协议 |
| 复用 OAuth 订阅 | 把 ChatGPT/Codex 订阅包装成 OpenAI 兼容 API，供 LiteLLM 调用 |
| 出网可控 | 哪些域名走节点、哪些直连，必须能随时调整且不用重启服务 |
| 凭据可持久 | OAuth 凭据、LiteLLM 配置、Postgres 数据在容器重建后不丢 |
| 运维可视化 | 节点切换、订阅刷新、账号登录、连通性验证要有网页界面，不依赖命令行 |

关键约束：**CLIProxyAPI 的 `proxy-url` 是全局开关，没有"按域名绕过"的能力**。
它一旦设了 `proxy-url`，所有出网请求都交给 mihomo。因此"分流"这件事只能落在 mihomo 规则层。
这是整个方案的设计支点，后文多处决策都由它推导出来。

---

## 2. 总体架构

```text
                    ┌──────────────── 宿主机 ────────────────┐
                    │                                        │
   浏览器/客户端 ────┼──▶ :4000  LiteLLM ──┐                  │
                    │                     │                  │
   运维浏览器 ──────┼──▶ :8787  proxy-console ─┐              │
                    │                         │              │
                    └─────────────────────────┼──────────────┘
                                              │
                    ┌──── compose 内部网络（不发布端口）────┐
                    │                         │              │
                    │        ┌────────────────┴───────┐      │
                    │        ▼                        ▼      │
                    │  cli-proxy-api:8317        proxy:9090   │
                    │   ├ /v1/*        数据面      （mihomo 控制面）
                    │   └ /v0/management 管理面      │        │
                    │        │                      │        │
                    │        │ proxy-url            │        │
                    │        ▼                      │        │
                    │   proxy:7890  ◀───────────────┘        │
                    │   （mihomo 数据面）                     │
                    └────────┼───────────────────────────────┘
                             ▼
                    ChatGPT / Codex 上游

        LiteLLM ──▶ db:5432 (PostgreSQL 16)
```

要点：

- **数据面与控制面分离**。`proxy` 是真正转发业务流量的 mihomo 进程；`proxy-console` 只是网页控制面，
  自己不转发任何业务流量。控制台挂掉不影响模型调用。
- **8317 / 7890 / 9090 全部不发布到宿主机**，只在 compose 网络内可见。外部能碰到的只有 4000 和 8787。
- **8787 是唯一的运维入口**，也是唯一能改代理规则的地方，权限等价于改 mihomo 配置（见第 7 节）。

---

## 3. 服务清单

| 服务 | 镜像 | 对外端口 | 职责 | 持久化 |
| --- | --- | --- | --- | --- |
| `proxy` | `alpine:3.20` + 仓库内 mihomo 二进制 | 无（7890/9090 内部） | mihomo 数据面：按规则分流、转发 OAuth/API 流量 | `./network` 挂载 |
| `cli-proxy-api` | `${CLI_PROXY_IMAGE:-eceasy/cli-proxy-api:v7.3.2}` | 无（8317 内部） | 把 Codex OAuth 订阅包装成 OpenAI 兼容 API；提供 OAuth 管理接口 | `cliproxy_auths` 卷 |
| `proxy-console` | `python:3.12-alpine` | **8787** | 节点/订阅/账号/例外清单的网页控制台 | `./network` 只读 + `./network/mihomo/ruleset` 可写 |
| `litellm` | `litellm/litellm-database:v1.100.1` | **4000** | 统一 API 网关、模型与虚拟密钥管理 | `postgres_data` 卷（经 db） |
| `db` | `postgres:16` | 无（5432 内部） | LiteLLM 的配置与用量数据库 | `postgres_data` 卷 |

依赖关系：`litellm` 等 `db` 健康后再起；`proxy-console` 和 `cli-proxy-api` 只等 `proxy` `service_started`
（不等 healthcheck，原因见 [REVIEW.md](REVIEW.md) 中 healthcheck 一节）。

---

## 4. 三条核心链路

### 4.1 模型调用链路

```text
客户端 ──Bearer LITELLM_MASTER_KEY ──▶ LiteLLM:4000/v1
                                        │  查库取模型配置
                                        ▼
                        POST http://cli-proxy-api:8317/v1/chat/completions
                        Authorization: Bearer $CLIPROXY_API_KEY
                                        │
                                        ▼  按 proxy-url 出网
                        http://proxy:7890 (mihomo)
                                        │
                                  命中规则？ ── 直连 ──▶ 上游
                                        └── 走节点 ──▶ 节点 ──▶ 上游
```

**LiteLLM 自己不认识"翻墙"这件事**。它只知道模型配的 `api_base` 是 `http://cli-proxy-api:8317/v1`，
是容器间直连地址。要不要翻墙由 CLIProxyAPI 那一跳决定——具体说是**上游域名**在 mihomo 里的规则判定结果。

> 因此 `docker-compose.yml` 里 `litellm` 刻意**不设** `HTTP_PROXY` / `HTTPS_PROXY`
> （[docker-compose.yml:114-122](docker-compose.yml#L114-L122) 有完整注释）。
> `NO_PROXY` 保留着，是为将来某个自有 endpoint 需要走节点时取消注释用的。

### 4.2 控制台链路

```text
运维浏览器 ──Basic Auth──▶ proxy-console:8787
                              ├─ HTTP ─▶ mihomo:9090          节点列表/切换/测速/订阅刷新
                              ├─ HTTP ─▶ cli-proxy-api:8317/v0/management   登录、账号、回调
                              ├─ HTTP ─▶ cli-proxy-api:8317/v1              仅连通性自检
                              └─ 文件 ─▶ /ruleset/*.list      代理例外清单读写
```

控制台是**纯标准库单文件**程序（`network/proxy-console.py`，2069 行，无第三方依赖），
前端 HTML/CSS/JS 完全内嵌在同一个文件里，所以 `python:3.12-alpine` 零依赖即可运行。

### 4.3 Codex OAuth 登录链路

```text
① 浏览器点「登录 Codex」
   proxy-console ──GET /codex-auth-url?is_webui=true──▶ CLIProxyAPI
                 ◀── { url, state } ──                （state 存在控制台内存里）
   proxy-console ──返回 url 给浏览器──▶ window.open 打开授权页

② 用户在 ChatGPT 完成授权
   回调有两种落地方式：
   ├─ 回调直达服务器 ──▶ CLIProxyAPI 收下 code（浏览器自动轮询状态即显示"已登录"）
   └─ 回调落到浏览器 localhost:1455（远程部署常见）
        └─ 用户复制地址栏完整 URL ──▶ 控制台「OAuth 回调地址」输入框
             └─ POST /v0/management/oauth-callback ──▶ CLIProxyAPI

③ 凭据落盘到 cliproxy_auths 卷（/root/.cli-proxy-api），容器重建不丢
```

第 ② 步的兜底路径是远程部署的关键：**不要把 1455 端口映射到公网**，
把回调 URL 手动贴回控制台即可（详见 [USAGE.md](USAGE.md)）。

---

## 5. 配置解析链路

密钥不写进 `docker-compose.yml`，而是集中在根目录 `.env`，经三层传递到运行时：

```text
.env                  （唯一明文密钥存放点，不进版本库）
  │
  ├─▶ docker compose 变量替换 ──▶ 容器环境变量
  │        LITELLM_MASTER_KEY / LITELLM_SALT_KEY / DATABASE_URL / CONSOLE_* ...
  │
  └─▶ CLIProxyAPI 特殊路径：
        .env 的 CLIPROXY_API_KEY
          │
          ▼
      cliproxyapi/entrypoint.sh 用 sed 把模板里的 __CLIPROXY_API_KEY__ 替换掉
          │
          ▼
      容器内 /CLIProxyAPI/config.yaml（运行时生成，不落版本库）
```

**为什么要走 entrypoint 渲染？** CLIProxyAPI 的 `api-keys` 字段不做环境变量展开
（`cliproxyapi/entrypoint.sh:16-20` 有注释说明）。而 `MANAGEMENT_PASSWORD` 是环境变量直接生效的
（见 `cliproxyapi/config.yaml.template:26-31` 的 `remote-management`）。
两条路径并存是有意的：数据面 key 走模板渲染，管理面 key 走环境变量。

CLIProxyAPI 模板里的两个关键配置：

```yaml
proxy-url: "http://proxy:7890"     # 所有上游流量交给 mihomo
request-retry: 3                   # 上游失败重试 3 次
quota-exceeded:
  switch-project: true             # 额度用尽自动换 project
  switch-preview-model: true       # 额度用尽自动降级到 preview 模型
remote-management:
  allow-remote: true               # 允许局域网调用管理接口（供控制台用）
  disable-control-panel: true      # 关掉 CLIProxyAPI 自带网页，统一走 8787
```

---

## 6. 密钥体系

五把钥匙，权限边界各不相同，**不能混用**：

| 密钥 | 存放 | 谁在用 | 权限范围 | 轮换影响 |
| --- | --- | --- | --- | --- |
| `LITELLM_MASTER_KEY` | `.env` | 客户端、LiteLLM 管理界面 | LiteLLM 全部虚拟密钥的管理权 | 可轮换 |
| `LITELLM_SALT_KEY` | `.env` | LiteLLM 内部 | 加密存储的模型凭据 | **有数据后永不可改**，必须备份 |
| `POSTGRES_PASSWORD` | `.env` | LiteLLM ↔ db | 数据库 | **仅首次初始化数据目录时生效**，只能走导出→删卷→重建→导入 |
| `CLIPROXY_API_KEY` | `.env` → entrypoint 渲染 | LiteLLM、控制台自检 | 只能调 `/v1/*` 数据面 | 需同时更新 CLIProxyAPI 与控制台并重启 |
| `CLIPROXY_MANAGEMENT_KEY` | `.env` → 环境变量 | 仅控制台 | `/v0/management` 全部管理操作：登录、账号、回调 | 需更新控制台与 CLIProxyAPI 并重启 |
| `CONSOLE_PASSWORD` | `.env` | 运维人员 | 8787 控制台（等价于改 mihomo 规则） | 随意轮换 |

设计上做得对的地方：`CLIPROXY_MANAGEMENT_KEY` 权限高于数据面 key，控制台**从不把它回传给浏览器**
（`network/proxy-console.py:118-153`，密钥只进请求头）；账号列表接口也只白名单抽取
`name/email/type/provider/status` 五个非敏感字段（`proxy-console.py:199-215`）。

---

## 7. 代理例外清单机制

这是本方案唯一的分流控制点。

### 7.1 为什么只能在这一层做

链路上有四个地方看起来能决定"走不走代理"，实际只有 mihomo 有效：

| 层次 | 作用范围 | 结论 |
| --- | --- | --- |
| **mihomo 规则** | 所有经 mihomo 的流量 | ✅ **唯一有效** |
| CLIProxyAPI 的 `proxy-url` | 全局生效，无按域名绕过开关 | ❌ 只能靠 mihomo 规则 |
| 容器 `HTTP_PROXY`/`NO_PROXY` | 只看容器自身请求，且只有认这两个变量的程序才认 | 当前没有容器设置（LiteLLM 已明确不设） |
| 宿主机 / docker daemon 代理 | 只影响 `docker compose pull` 等宿主机命令 | 与业务链路无关 |

**推论**：按模型分流，最终就是**按上游域名分流**。上游域名不同的模型可以放进不同清单；
上游域名相同的模型区分不了——mihomo 只看域名，不看账号。

### 7.2 两个清单

| 清单 | 文件 | 命中行为 | 典型用途 |
| --- | --- | --- | --- |
| 直连例外 | `network/mihomo/ruleset/CustomDirect.list` | 走真实 IP 直出，不经任何节点 | 内网域名、必须直连的域名 |
| 强制代理 | `network/mihomo/ruleset/CustomProxy.list` | 强制从「🚀 节点选择」出去 | 纠正被 `ChinaDomain`/`GEOIP,CN` 误判成国内、实际需要翻墙的域名 |

两个清单当前状态：`CustomDirect.list` 有 1 条实际规则（内网域名 `aix-backup.hismarttv.com`），
`CustomProxy.list` 为空集（只有注释）。

### 7.3 规则优先级

mihomo 是**首个匹配生效**。两个例外清单必须排在 `rules:` 最前面
（`network/mihomo/config.yaml:72-73`），否则会被 `ProxyLite` / `ChinaDomain` / `GEOSITE,CN` / `MATCH` 抢先命中：

```yaml
rules:
  - RULE-SET,CustomDirect,DIRECT                    # ① 直连例外
  - RULE-SET,CustomProxy,🚀 节点选择                 # ② 强制代理
  - DOMAIN,sub.ssrsub.de,DIRECT                    # ③ 订阅域名直连（防更新环路）
  - DOMAIN,link.ssrsub.de,DIRECT                   # ④ 同上
  - RULE-SET,LocalAreaNetwork,🎯 全球直连            # ⑤ 之后才是 ACL4SSR 的常规分流
  ...                                              #    （广告拦截 / 分类 / GeoIP）
  - MATCH,🐟 漏网之鱼                                # ㉑ 兜底
```

> ⚠️ **这个优先级是脆弱的**：任何在文件顶部插入规则、或调换 rule-provider 声明的改动，
> 都会让例外清单静默失效——网页上看起来配好了，实际不生效。
> 改 `config.yaml` 的 `rules:` 段后请务必在控制台确认「已加载 N 条」对得上。

### 7.4 生效方式

控制台写入 `.list` 文件后，通过 mihomo 控制接口让它重读，**不需要重启容器**：

1. 首选 `PUT /providers/rules/CustomDirect` — 只刷新规则集，干净。
2. 该接口在部分 mihomo 版本返回 404，此时降级为 `PUT /configs?force=true` 整份重载配置
   （`proxy-console.py:835-870`）。**副作用：会重置节点选择**，控制台会提示，但容易忽略。

---

## 8. 数据流：mihomo 的 DNS 与 fake-ip

理解排障手段需要先理解这层配置（`network/mihomo/config.yaml:11-23`）：

- `enhanced-mode: fake-ip`，`fake-ip-range: 198.18.0.1/16`
- `respect-rules: true` — DNS 解析也遵守分流规则
- `direct-nameserver` — 直连域名的解析走阿里 DoH

由此得到一个**廉价的分流判定手段**：问 mihomo 的 DNS，拿到 `198.18.x.x` 说明走节点，
拿到真实 IP 说明直连（命令见 [USAGE.md](USAGE.md)）。

需要注意的陷阱：**若某个"不需要代理"的上游只在别的容器 `/etc/hosts` 里可解析，
mihomo 会解析失败**——这种域名必须放进该容器的 `NO_PROXY`，不能只加直连规则。
本工程中 `aix-backup.hismarttv.com`（指向宿主机）就是这类。

---

## 9. 文件清单与持久化

```text
docker-compose.yml                    五个服务、内部网络、日志策略
.env                                  全部明文密钥（不进版本库）
ARCHITECTURE.md / USAGE.md / REVIEW.md  本文档及配套

cliproxyapi/
  config.yaml.template                CLIProxyAPI 基础配置（不含真实密钥）
  entrypoint.sh                       启动时渲染 CLIPROXY_API_KEY

network/
  proxy-console.py                    控制台（后端 + 内嵌前端，单文件）
  mihomo/config.yaml                  mihomo 订阅、规则、例外规则集声明、external-controller
  mihomo/ruleset/CustomDirect.list    ⬅ 控制台读写
  mihomo/ruleset/CustomProxy.list     ⬅ 控制台读写
  mihomo/ruleset/*.list               其余 15 个由 mihomo 按 1h 周期自动覆写
  mihomo/providers/subscription.yaml  订阅缓存（含节点凭据）
  mihomo-linux-amd64                  mihomo 二进制

  ── 以下为遗留资产，容器方案不使用 ──
  proxy-console.py 之外的 *.sh         宿主机裸进程的管理脚本
  proxy-nodes.py                       上述脚本的辅助工具
  clash/                               Clash Verge 的旧配置与安装包
```

命名卷：

| 卷 | 内容 | 备份要求 |
| --- | --- | --- |
| `cliproxy_auths` | Codex/Anthropic OAuth 凭据 | **必须备份**，丢了要重新登录 |
| `cliproxy_logs` | CLIProxyAPI 日志 | 可选 |
| `cliproxy_plugins` | CLIProxyAPI 插件 | 可选 |
| `postgres_data` | LiteLLM 配置与用量 | **必须备份** |

> ⚠️ `docker compose down -v` 会删除上述全部卷。**除非确认已备份，否则不要执行。**

---

## 10. 与遗留资产的关系

`network/` 下还存在一套**宿主机裸进程**的管理脚本（`proxy-start.sh`、`proxy-switch.sh`、
`proxy-status.sh` 等 6 个 + `proxy-nodes.py`），以及 `network/clash/` 目录
（83MB 的 Clash Verge 安装包 + 一份内联全部节点凭据的旧配置）。

**它们不属于当前方案**：

- 脚本默认 `MIHOMO_CONTROLLER_URL=http://127.0.0.1:9090`，而容器里的 9090 不发布到宿主机，
  所以从宿主机跑这些脚本**连不到容器内的 mihomo**。它们只能管理宿主机裸进程。
- `proxy-start.sh` 自己也做了让路探测，检测到 7890 已被占用就提示"请用容器方式管理"并退出。
- compose、控制台、README 均未引用 `clash/` 目录，它是被 mihomo 订阅方案取代的早期产物。
- 两套资产并存会造成"改了脚本却不生效"的困惑，建议归档或删除（见 [REVIEW.md](REVIEW.md)）。

---

## 11. 安全边界（设计前提）

必须在文档里说清楚的前提：

1. **只发布 8787 和 4000**。7890 / 8317 / 9090 一律不映射到宿主机。
2. **8787 是 Basic 鉴权 + 明文 HTTP**，没有 TLS、没有 CSRF token。
   能登进 8787 就能改代理规则、切节点、发起真实的上游生成请求——权限等价于改 mihomo 配置。
   → 设置强密码，限制防火墙来源网段，**不要暴露到公网**。
3. **9090（mihomo 控制口）无鉴权**，安全完全依赖"compose 不发布 9090"这一事实。
   任何能进入 compose 网络的进程都可以无凭证改规则、切节点、重载配置。
4. **`CLIPROXY_MANAGEMENT_KEY` 权限高于模型 API key**，只给控制台用，不要发给客户端。
5. `.env`、`cliproxy_auths` 卷内的 OAuth JSON、`network/mihomo/config.yaml` 的订阅 URL、
   `network/clash/RihD9ROJzl50.yaml` 的节点凭据都是敏感信息。
