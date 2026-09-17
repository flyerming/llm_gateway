# 问题分析与改进建议

> 本文是对当前工程的静态审查结论。配套文档：[ARCHITECTURE.md](ARCHITECTURE.md)（方案设计）、
> [USAGE.md](USAGE.md)（使用手册）。
>
> **审查范围**：`docker-compose.yml`、`.env`、`.gitignore`、`cliproxyapi/*`、
> `network/proxy-console.py`（2069 行，全文）、`network/mihomo/*`、`network/clash/*`、
> 遗留 shell 脚本。**未做**运行时压测与容器内验证（本机无 Docker，且不触碰线上环境）。

---

## 一、总体评价

**这套方案的设计是对的，而且不少细节比常见的自建网关更讲究。**

核心判断——「把分流决策收敛到 mihomo 单点，让 LiteLLM 保持无代理的干净状态」——
解决了一个真实存在的复杂度：CLIProxyAPI 的 `proxy-url` 是全局开关，没有按域名绕过能力。
方案没有去和这个限制对抗，而是把它变成了唯一的、可解释的决策点，并在文档里反复讲清楚了。
这是一个**想明白了再动手**的设计，不是拼凑出来的。

代码质量同样高于预期。控制台单文件、纯标准库、密钥不回传浏览器、规则输入严格校验、
文件原子替换、写探针验证可挂载性、常量时间比较口令——这些都是有经验的人才会写的防线。
尤其「**连通性自检**」区分「服务活着」和「凭据能用」并分级呈现上游原文，
这是运维视角的设计，很实用。

**主要短板不在架构，在收尾**：仓库里还留着两套并行的旧资产（宿主机脚本、`clash/` 目录），
几处安全配置停在"能用就行"的状态（`.env` 权限 644、9090 无鉴权、密钥复用），
文档与实物有几处对不上（镜像版本、Postgres 密码注释）。
这些都不影响功能，但会影响交接——**下一个人接手时，很可能踩到的就是这几处**。

成熟度评估：**功能完备度高，工程收尾度中等**。

| 维度 | 评价 |
| --- | --- |
| 架构设计 | ⭐⭐⭐⭐⭐ 决策清晰，边界明确，有文档支撑 |
| 功能完备度 | ⭐⭐⭐⭐⭐ 部署/运维/排障闭环齐全 |
| 代码质量 | ⭐⭐⭐⭐☆ 高于平均，少数健壮性缺口 |
| 安全配置 | ⭐⭐⭐☆☆ 设计对，执行松（权限/鉴权/密钥复用） |
| 可交接性 | ⭐⭐⭐☆☆ 有冗余资产与文档不一致，易误导 |
| 可观测性 | ⭐⭐⭐⭐☆ 分级自检是亮点，缺结构化日志与监控指标 |

---

## 二、优势

### A1. 分流决策收敛为单点，且被文档讲透

全链路只有 mihomo 规则能决定走不走代理，其余三处（CLIProxyAPI `proxy-url`、
容器 `HTTP_PROXY`、宿主机代理）要么全局、要么无关。
`docker-compose.yml:114-122` 用注释完整说明了"为什么 LiteLLM 不设代理"，
`network/README.md:124-148` 用表格逐层对比。**这种把"为什么不做某事"写进配置注释的习惯，是本工程最好的部分。**

### A2. 数据面与控制面彻底分离

`proxy`（mihomo 数据面）与 `proxy-console`（控制面）是两个容器，
控制台挂掉不影响模型调用；`8317`/`7890`/`9090` 全部不发布到宿主机。
故障域划分干净。

### A3. 密钥治理的代码实现规范

- 管理密钥只进请求头，**从不写入任何响应**（`proxy-console.py:118-153`）
- 账号接口白名单抽取 5 个非敏感字段，注释明说不返回 token（`proxy-console.py:199-215`）
- 口令用 `hmac.compare_digest` 常量时间比较（`proxy-console.py:1835-1837`），防时序爆破
- 数据面/管理面两把钥匙用途分离，文档明确"不能相同、不能发给客户端"

### A4. 输入校验封死了注入面

- **无命令注入**：全文无 `os.system` / `subprocess` / `eval` / `exec`
- **无路径穿越**：`which` 参数经字典映射到固定文件名（`proxy-console.py:745-749`），
  不参与路径拼接
- 规则值白名单校验并拒绝空格/逗号/引号/换行（`proxy-console.py:668-711`），
  杜绝了往规则文件注入额外规则行
- URL 拼接前 `urllib.parse.quote(x, safe="")` 转义（`proxy-console.py:1987`）

### A5. 文件写入的健壮性处理

`write_ruleset()` 先写 `.tmp` 再 `os.replace` 原子替换（`proxy-console.py:771-804`），
避免 mihomo 读到半截文件；同时保留文件头部注释块。
`ruleset_writable()` 不只信 `os.access`（root 对只读挂载也返回可写），
而是实写探针文件验证（`proxy-console.py:635-654`）。**这两处都是踩过坑才会写的代码。**

### A6. 刻意的禁代理 opener

`_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))`（`proxy-console.py:89`），
所有出站请求共用。防止 `HTTP_PROXY` 环境变量泄漏导致"访问本机控制接口却绕道代理"的死锁。
这个小细节很少有人会想到。

### A7. 分级自检设计（本工程最实用的运维设计）

「连通性自检」证明服务活着，「实际生成一次」证明凭据能用，并明确写出
**"第一步通过不代表能用"**。排障时贴出上游原文而非猜测（`proxy-console.py:323-450`）。
这比绝大多数自建网关的 `/health` 端点有用得多。

### A8. 防环路与降级设计

- 订阅域名显式直连（`network/mihomo/config.yaml:74-75`），防止订阅更新自身被代理形成环路
- mihomo 规则集刷新接口 404 时降级为整份重载，并**返回标记告知前端**会重置节点选择
  （`proxy-console.py:835-870`）
- `provider_info()` 对 404 与缺字段都做成可降级返回 `{supported: False, reason: ...}`
  （`proxy-console.py:540-585`），兼容不同 mihomo 版本

### A9. 依赖健康检查的取舍正确

`cli-proxy-api` / `proxy-console` 只依赖 `proxy` 的 `service_started` 而非 `service_healthy`，
避免了 Alpine busybox `wget` 参数差异导致 healthcheck 误报、进而阻塞整个启动链的问题。
`docker-compose.yml:36` 的注释也预告了这个坑。

---

## 三、问题清单

按「处理优先级」而非「严重程度」排序。P0 是**现在就该处理**的，
P1 是**影响可运维性/可交接性**的，P2 是**有余力再改**的。

---

### P0 — 安全，建议立即处理

#### P0-1. 敏感凭据未被 `.gitignore` 覆盖，且项目尚无版本控制

| 位置 | 内容 |
| --- | --- |
| `network/mihomo/config.yaml:30` | 订阅 URL 中的令牌（拿到即可拉取全部节点） |
| `network/clash/RihD9ROJzl50.yaml:20+` | 128+ 节点的 uuid / 密码 / 服务器地址 |

`.gitignore:10-12` **只写了注释提醒，没有对应的忽略规则**——
它实际忽略的是 `providers/`、`cache.db`、`mihomo.log`（`.gitignore:6-8`），
**不含**上面两个文件。

更关键的是：**当前目录不是 git 仓库**（无 `.git`），等于完全没有版本控制。
`.gitignore` 处于"备而不用"状态——一旦有人 `git init && git add .`，
这两个文件会连同 `.env` 之外的全部凭据一起进库。

**建议**（按顺序）：

1. 立即把两个文件加入 `.gitignore`：

   ```gitignore
   # 含明文凭据，永不入库
   network/mihomo/config.yaml
   network/clash/
   ```

2. 若确实要给 `config.yaml` 做版本控制，抽出订阅 URL 到独立文件（如
   `network/mihomo/subscription.url`，一并 ignore），`config.yaml` 改用占位符并在
   启动时渲染——可复用 `cliproxyapi/entrypoint.sh` 已有的 sed 渲染模式。
3. 把 `network/clash/` 整体归档或删除（见 P1-7）。
4. 若订阅令牌已随文件外发过，**直接去机场后台重置订阅链接**，成本最低。

#### P0-2. `LITELLM_MASTER_KEY` 与 `POSTGRES_PASSWORD` 是同一个值

`.env:6` 与 `.env:14` 完全相同。

**风险**：Postgres 密码会出现在 `DATABASE_URL`（`docker-compose.yml:112`）中，
即可见于 `docker compose config` 输出、进程环境和部分日志;
而 `LITELLM_MASTER_KEY` 是 LiteLLM 全部虚拟密钥的管理权。
两把钥匙之一泄漏即等于双泄漏，违背了最小权限原则。

**实践上风险不高**（同机部署，无外部暴露），**但违反"密钥不复用"这条基线**，
且轮换其中一把会连带影响另一把。

**建议**：改 `LITELLM_MASTER_KEY` 为新随机值（可随时轮换，
客户端与 LiteLLM 管理界面同步更新即可）。`POSTGRES_PASSWORD` 涉及数据卷重建，见 P1-5，暂缓。

#### P0-3. `.env` 文件权限为 `-rw-r--r--`

同机其他用户可读全部密钥。若服务器是多用户环境（或有低权限运维账号），构成实际泄漏面。

**建议**：`chmod 600 .env`，并把这条写进部署步骤（已写入 [USAGE.md](USAGE.md#21-准备-env)）。

#### P0-4. mihomo 控制口 `9090` 无鉴权

`network/mihomo/config.yaml:10` 为 `external-controller: '0.0.0.0:9090'`，
**全文没有 `secret:` 字段**（属刻意设计，`config.yaml:6-9` 有注释说明）。

安全性完全依赖"compose 不发布 9090"这一约定。任何能进入 compose 网络的进程
（包括将来误加的服务、被投毒的镜像）都可**无凭证**调 `/proxies` 切节点、
`PUT /configs?force=true` 重载配置。属于"防御只有一层，且是约定而非机制"。

**建议**（低成本，收益明确）：

```yaml
# network/mihomo/config.yaml
external-controller: '0.0.0.0:9090'
secret: '__MIHOMO_SECRET__'      # 与 .env 的 MIHOMO_SECRET 一致
```

同步让 `proxy-console` 带上它——`proxy-console.py:457-482` 的 `mihomo()` 目前
**不带任何鉴权头**，需加一处 `Authorization: Bearer <secret>`，
并在 compose 里给控制台注入 `MIHOMO_SECRET`。
若不想引入新密钥，也可让 mihomo 只监听 `0.0.0.0` → 改为容器网络别名下的绑定。

#### P0-5. 控制台 8787 无 TLS、无 CSRF 防护、无速率限制

`network/README.md:266` 已承认"Basic 鉴权是唯一的局域网入口"，三点事实：

1. **Basic 凭据以 base64 明文过局域网**（无 TLS）。局域网内嗅探即得口令。
2. **无 CSRF token**。所有副作用操作都是 POST（GET 全部只读，这点做对了），
   但 `_body()`（`proxy-console.py:1859-1871`）不校验 `Content-Type`，
   跨站 `fetch` 配合 `text/plain` 简单请求即可构造合法 JSON body，
   理论上可在管理员浏览恶意页面时静默改规则集/切节点。
   现代浏览器 Private Network Access 会拦截"公网页面 → 私网 IP"，是主要缓解。
3. **无速率限制**。`POST /api/cliproxy/selftest?probe=true` 每次都真实调用付费上游
   （`proxy-console.py:408-443`），口令泄漏后可被反复触发刷额度。

**建议**：

- 短期（不改代码）：防火墙限制 8787 来源网段；`CONSOLE_PASSWORD` 用高强度值
- 中期：`_body()` 增加 `Content-Type: application/json` 校验并拒绝跨站简单请求
- 长期：前置一个带 TLS 的反向代理（Caddy/nginx），或改用带 Cookie 鉴权的方案

---

### P1 — 影响可运维性与可交接性

#### P1-1. `POSTGRES_PASSWORD` 注释与实际值不符（**最危险的文档不一致**）

`.env:10-13` 的注释写着：

> 现有的 postgres_data 卷是用 "litellm" 初始化的，所以这里必须保持 litellm

但 `.env:14` 的实际值是 `sk-79d3...`（与 master key 同值）。

两者必有一错。既然服务当前运行正常，**大概率是数据卷已用新密码重建过，
而注释没跟着更新**。危险在于：下一个运维人员读到这条注释，
可能会把值"改回" `litellm`，导致 LiteLLM 认证失败——
而 `pg_isready` 健康检查不校验密码，症状是**容器显示健康但运行时报认证失败**，
极易误判为别的问题。注释自己也预告了这个陷阱。

**建议**（**本次未改动**，因为它涉及线上运行中的凭据）：

1. 确认当前实际生效的密码：`docker compose exec db psql -U litellm -c '\conninfo'`
   或在 `litellm` 容器里验证 `DATABASE_URL` 能连上
2. 确认后**删掉或改写这段注释**，改为描述事实：

   ```dotenv
   # 本值仅用于初始化数据目录 / 生成 DATABASE_URL。修改它不会改变已有卷的密码，
   # 反而会让 LiteLLM 连不上（症状：容器健康但运行时报认证失败）。
   # 当前值对应的是已初始化的 postgres_data 卷，请勿随意改动。
   POSTGRES_PASSWORD=<实际值>
   ```

3. 若确实需要换成强随机密码，走「导出 → 删卷 → 重建 → 导入」流程
   （注意：`docker compose down -v` **不是**迁移命令）

#### P1-2. README 中的镜像版本与实际不符

- `network/README.md:30`：`eceasy/cli-proxy-api:v7.2.157`
- `docker-compose.yml:47`：`${CLI_PROXY_IMAGE:-eceasy/cli-proxy-api:v7.3.2}`

**建议**：README 改为「默认 `v7.3.2`，可用 `.env` 的 `CLI_PROXY_IMAGE` 覆盖」。

#### P1-3. 控制台的 `_ruleset_writable` 一旦为 False 永不复位

`proxy-console.py:641-654` 只在首次调用时计算（`if _ruleset_writable is None`），
`proxy-console.py:803` 写入失败后直接置 `False`，**没有任何 TTL 或重试复位**。

**后果**：挂载权限问题修好后，网页上「添加规则」按钮仍然是灰的，
必须**重启容器**才能恢复。运维现场会表现为"我明明修好了它还是不让改"。

**建议**：给缓存加 TTL（如 30 秒），或提供 `POST /api/rules/apply` 顺带重置该标志。

#### P1-4. OAuth state 会话表存在内存泄漏

`_oauth_sessions`（`proxy-console.py:90`）的过期条目**只在被访问时清理**
（`proxy-console.py:161-171`）。

**触发条件**：用户点击「登录 Codex」后直接关掉页面，不再轮询
`/api/cliproxy/status` → 该 state 永久驻留字典。长期运行会缓慢增长
（虽然有 900 秒 TTL 字段，但不清理就等于没有）。

**建议**：在 `_remember_oauth()` 里顺手清理过期项，或加一个后台清理线程。

#### P1-5. HTTP 服务端缺 socket 超时与请求体上限（Slowloris 面）

已核实的三点：

- `ThreadingHTTPServer` + `daemon_threads = True`，但**未设置 `server.timeout`，
  `Handler` 也没有类级 `timeout`**（`proxy-console.py:2052-2053`）
- `self.rfile.read(length)` 的 `length` 来自请求头 `Content-Length`，**无上限校验**
  （`proxy-console.py:1846-1850`、`1861-1866`）
- 每连接一线程，无连接数上限

**后果**：声明超大 `Content-Length` 即可让线程卡在 read 上；
配合局域网可达性，构成线程耗尽的 DoS 面。个人内网工具风险有限，
但修复成本极低。

**建议**：三行代码即可

```python
class Handler(BaseHTTPRequestHandler):
    timeout = 30            # 类级 socket 超时
    MAX_BODY = 1 << 20      # 1 MiB
```

并在 `_body()` / `_require_auth()` 里对 `length > MAX_BODY` 直接回 413。

#### P1-6. 前端两处错误处理会误导用户（**最影响日常体验的问题**）

**a) OAuth 轮询把「state 过期」误报成「等待回调」**（已核实，`proxy-console.py:1412-1414`）：

```js
} catch (e) {
  setCodexStatus('等待回调…', 'meta warn');   // ← 任何错误都当成"再等等"
}
```

服务端对过期 state 会返回 400「OAuth state 无效或已过期」，
但页面永远显示「等待回调…」。**用户会一直等下去**——这正是 [USAGE.md](USAGE.md#53-确认登录真的生效重要)
里那个"登录卡住"场景的真实成因之一。应区分错误类型：state 过期应停止轮询并提示重新发起登录。

**b) 30 秒定时轮询静默吞掉全部异常**（已核实，`proxy-console.py:1784-1785`）：

```js
setInterval(() => {
  if (!busy && state) load(state.group).catch(() => {});   // ← 空 catch
  if (!busy) loadRules().catch(() => {});
}, 30000);
```

控制台后端挂掉或 mihomo 不可达时，页面**停留在旧数据且没有任何提示**，
用户会以为看到的是实时状态。建议改为把失败状态显示在顶部状态栏
（初始化路径 `proxy-console.py:1776-1782` 已经这么做了，轮询路径应保持一致）。

#### P1-7. 冗余资产造成"两套方案谁是权威"的困惑

**a) 宿主机 shell 脚本**（`proxy-*.sh` 6 个 + `proxy-nodes.py`）

这些脚本默认 `MIHOMO_CONTROLLER_URL=http://127.0.0.1:9090`
（`proxy-status.sh:11`、`proxy-switch.sh:7`），而容器里的 9090 **不发布到宿主机**——
所以从宿主机跑这些脚本**连不到容器内的 mihomo**，只能管理宿主机裸进程。
`proxy-start.sh:32-39` 也自己做了让路探测，检测到 7890 被占用就提示"请用容器方式管理"。

`network/README.md:187-198` 的文件清单**没有收录这些脚本**，文档与实物脱节。

**b) `network/clash/` 目录**（83 MB）

- `Clash_Verge_2_5_1_amd64.deb`（83 MB 安装包）
- `RihD9ROJzl50.yaml`：内联全部节点凭据的旧配置，`rules` 段**没有**例外清单机制

compose、控制台、README **均未引用**该目录，是被 mihomo 订阅方案取代的早期产物。

**建议**：

1. 建 `legacy/` 目录归档，或直接删除
2. 若保留，在 `network/README.md` 明确标注「仅用于无 Docker 的宿主机场景，与容器方案无关」
3. `clash/` 至少先移出部署目录（含明文凭据，见 P0-1）

#### P1-8. 例外清单的优先级脆弱性没有任何自动化保护

`CustomDirect` / `CustomProxy` 必须在 `network/mihomo/config.yaml:72-73`（`rules:` 最前两条），
否则被 `ProxyLite` / `ChinaDomain` / `GEOSITE,CN` / `MATCH` 抢先命中，
**网页上看起来配好了却不生效**（`config.yaml:68-71` 注释已警示）。

当前没有任何校验机制。任何人在 `rules:` 顶部插入一条规则就会静默破坏这个不变量。

**建议**：在 `proxy-console.py` 启动时或 `/api/rules` 里加一个检查——
读 `MIHOMO_CONFIG_PATH`（该变量已存在，`proxy-console.py:79`，目前只用于降级重载），
解析 `rules:` 段，确认前两条是这两个 RULE-SET，否则在面板顶部显示醒目警告。
这是一个**纯增量、无副作用**的改进，能让最隐蔽的配置陷阱变成显式提示。

#### P1-9. 整份重载的副作用没有日志留痕

`apply_ruleset()` 降级走 `PUT /configs?force=true` 时会**重置节点选择**
（`proxy-console.py:858-868`），仅在返回值标记 `reloaded_config`。
**服务端日志没有任何警告**（`log_message()` 只记录请求行，`proxy-console.py:1877-1879`）。

排障时"节点莫名变回默认"很难归因。

**建议**：降级分支加一行 stderr 日志：「警告：规则集刷新降级为整份配置重载，节点选择已重置」。

---

### P2 — 健壮性与代码整洁

| # | 问题 | 位置 | 建议 |
| --- | --- | --- | --- |
| P2-1 | 自检把上游**响应原文**（截断 1500 字符）回传浏览器。若上游在报错里回显 API key 即外泄。虽然罕见，但注释声称"绝不外泄"与实际实现有出入 | `proxy-console.py:254`、`358`、`440` | 对原文做一次 key 擦除再返回，或在注释里修正表述 |
| P2-2 | 响应头缺 `X-Content-Type-Options: nosniff`、`Content-Security-Policy`、`X-Frame-Options`。前端有 `esc()` 转义且未发现注入点，但缺 CSP 是纵深防御缺口 | `proxy-console.py:1803-1811` | 补三个安全头，成本极低 |
| P2-3 | 未实现 `do_HEAD` / `do_OPTIONS`，会由基类返回 **501 且绕过鉴权** | — | 健康检查若用 HEAD 会拿到 501，建议显式实现并纳入鉴权 |
| P2-4 | `cliproxy_selftest` 两条返回路径结构不一致：非 probe 分支缺 `accounts` 键 | `proxy-console.py:395-399` vs `445-450` | 统一返回结构 |
| P2-5 | `ok` 用 `all(... for step in steps if not step.get("skipped"))`，**所有步骤都被 skip 时 `all([]) == True`** 会报"全部通过" | 同上 | 增加"至少一个步骤真正执行过"的判断 |
| P2-6 | 超时值分散硬编码：10/15/20/30/60/90/120 秒散布各处，无集中常量 | 全文 | 抽到文件头部常量区 |
| P2-7 | `_speedtest` 不校验 group 是否存在，不存在的分组会走到 mihomo 404，被映射成"这个 mihomo 版本不支持分组批量测速"——**错误信息误导** | `proxy-console.py:2012-2027` | 先校验 group |
| P2-8 | 每次 `/api/state` 对 mihomo 发 **2 个**请求（`build_state` 内额外调 `provider_info()`），前端 30 秒轮询一次 → 空闲时 QPS 恒定不为零 | `proxy-console.py:984` | provider 信息可加缓存或用更长的轮询周期 |
| P2-9 | `_refresh()` 刷新后立刻再发一次 `/providers/proxies`，与上一条叠加 | `proxy-console.py:2009` | 复用刷新接口的返回 |
| P2-10 | `do_HEAD`/`do_OPTIONS` 之外，`Handler` 未设 `protocol_version`，默认 **HTTP/1.0**，每请求新建 TCP 连接（也不发 keep-alive） | — | 设 `protocol_version = "HTTP/1.1"` 并确保 `Content-Length` 正确（当前已正确） |
| P2-11 | `log_message` 把完整请求行写入日志，含 `/api/cliproxy/status?...&state=<state>` 的 state 值 | `proxy-console.py:1877-1879` | state 非凭据但属会话标识，建议脱敏 |
| P2-12 | `NO_PROXY_BASE` 硬编码容器名（`localhost,127.0.0.1,db,proxy,cli-proxy-api`），改 compose 服务名即失配 | `proxy-console.py:626` | 从环境变量读取 |
| P2-13 | 测速超时硬编码 `timeout_ms = 5000`，不可配；而 `HEALTHCHECK_URL` 可配，二者不一致 | `proxy-console.py:2014` | 加环境变量 |
| P2-14 | JS 转义逻辑重复三份（`esc()` 定义在 1537，另有两处内联实现同样的字符替换），且其中一处的 `"` 转义是冗余的 | `proxy-console.py:1311-1313`、`1342-1349` | 统一调用 `esc()` |
| P2-15 | 三个 HTTP 客户端（`_raw_request` / `mihomo` / `cliproxy`）的 try/except + JSON 解析 + 空响应处理高度雷同 | `257`、`457`、`118` | 抽公共函数 |
| P2-16 | `main()` 的 `rpartition(":")` 解析监听地址**不支持 IPv6**（`[::]:8787` 会得到 host=`[::]`，绑定可能失败） | `proxy-console.py:2043-2045` | 用 `ipaddress` 或特判方括号 |
| P2-17 | `apply_ruleset()` 不在 `_ruleset_lock` 内，与并发的 `/api/rules/add` 可能交错 | `proxy-console.py:835-870` | 加锁（影响有限，写回本身是原子的） |
| P2-18 | `_body()` 不校验 `Content-Type`（见 P0-5） | `proxy-console.py:1859-1871` | 见 P0-5 |
| P2-19 | `rules_state()` 里 `lists["direct"]["rules"]` 依赖上游隐式预置的 `"rules": []`，重构时易 500 | `proxy-console.py:942` | 用 `.get("rules", [])` |
| P2-20 | `postgres:16` 与宿主上可能存在的 18.4 数据卷不能直接互用，当前只在 `network/README.md:208-210` 有说明 | `docker-compose.yml:135` | 保留该说明，并补进 [USAGE.md](USAGE.md) 的升级章节 |
| P2-21 | `__pycache__/proxy-console.cpython-313.pyc` 残留在 `network/`（`.gitignore` 已忽略，但目录本身不应存在） | `network/__pycache__/` | 删除 |
| P2-22 | 文档字符串（`proxy-console.py:13-27`）未列出 `DIRECT_RULE_PROVIDER` / `PROXY_RULE_PROVIDER` / `CLIPROXY_OAUTH_SESSION_TTL` 三个环境变量 | `proxy-console.py:76-85` | 补齐 |

---

## 四、改进路线图

### 第一批：安全收尾（半天，零风险）

| 项 | 动作 | 影响面 |
| --- | --- | --- |
| P0-3 | `chmod 600 .env` | 无 |
| P0-1 | 补 `.gitignore` 两行；`clash/` 移出部署目录 | 无 |
| P0-2 | `LITELLM_MASTER_KEY` 换新值 | 客户端与管理界面同步更新 |
| P1-1 | 核实 Postgres 实际密码，改写 `.env` 注释 | 仅注释 |
| P1-2 | 修正 README 镜像版本 | 仅文档 |
| P2-21 | 删 `network/__pycache__/` | 无 |

### 第二批：低风险代码改进（1 天）

- **P1-6**（前端错误提示）—— 直接改善日常体验，用户最易感知
- **P1-3**（`_ruleset_writable` 加 TTL）
- **P1-4**（OAuth 会话清理）
- **P1-5**（socket 超时 + body 上限，三行代码）
- **P1-9**（降级重载加日志）
- **P2-2**（安全响应头）
- **P2-1**（自检原文做 key 擦除）

### 第三批：可维护性（2–3 天）

- **P1-8**（例外清单优先级自检）—— 收益最高的一项，把最隐蔽的配置陷阱变成显式提示
- **P1-7**（归档遗留脚本与 `clash/`，并更新 README 文件清单）
- **P0-4**（给 mihomo 9090 加 secret）+ 目前控制台无鉴权头，需同步改
- **P0-5**（`Content-Type` 校验 + 前置 TLS 反代）
- P2 中的超时集中化、`esc()` 去重、HTTP 客户端抽取

### 第四批：可选增强

- 结构化日志（JSON 格式）与关键指标导出（活跃节点延迟、上游错误率、自检成功率）
- `proxy-console.py` 按职责拆分（当前 2069 行单文件，前端占 800 行；
  内嵌便于分发，但已接近维护成本拐点）
- 模型级的用量/成本看板（LiteLLM 已有数据基础）

---

## 五、一句话结论

**方案设计值得保留，收尾工作值得投入。**
第一批 + 第二批加起来约 1.5 天，能把当前「能跑但交接有风险」的状态
提升到「能跑且新人也敢动」——而第三批的 P1-8（优先级自检）
是本工程投入产出比最高的一项改进。
