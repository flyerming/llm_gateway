# DeepSeek Harness 多用户 Docker 部署

本目录是根项目对 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
的部署封装，不是 DeepSeek Harness 上游源码本身。

目标是把 `dsh web` 纳入根目录的 `docker compose`，并提供一套适合局域网/内网穿透
使用的多用户入口：

```text
浏览器
  │  :4000/deepseek-harness/，Basic Auth + WebSocket
  ▼
统一入口 Nginx（宿主机只发布 :4000）
  │  /deepseek-harness/ → deepseek-harness:3080
  │  其他路径           → litellm:4000
  ▼
DeepSeek Harness 容器内 Nginx（0.0.0.0:3080）
  │  auth_request，按用户名分配端口
  ▼
provisioner（容器内 127.0.0.1:3090）
  │  首次访问时创建用户目录并启动该用户自己的 DSH
  ├── alice → DSH 127.0.0.1:310xx → /workspaces/alice
  └── bob   → DSH 127.0.0.1:310yy → /workspaces/bob
             每个用户拥有独立的 DSH_HOME
  │
  ▼
LiteLLM（compose 服务名 litellm:4000）
  │
  ▼
CLIProxyAPI / 其他已接入 LiteLLM 的模型
```

## 1. Git 边界：官方源码不改、不提交

当前目录里可以存在一个本地上游源码 checkout：

```text
deepseek-harness/
├── README.md                              # 本项目跟踪
├── Dockerfile                             # 本项目跟踪
├── nginx.conf                             # 本项目跟踪
├── provision.py                           # 本项目跟踪
├── start.sh                               # 本项目跟踪
├── settings.yaml                          # 本项目跟踪
└── deepseek-harness-dsh-v0.1.5-rc.2-git/  # 本地 checkout，不提交、不修改
```

上游 checkout 自带 `.git`。如果把它直接 `git add` 到根项目，根 Git 会把它记录成
`160000` 的嵌套仓库/gitlink，而不会跟踪里面的普通文件；这不是 Git 失效。
根目录 `.gitignore` 现在明确忽略该 checkout、依赖包以及运行时数据。

本项目的 Dockerfile **不会复制本地上游 checkout**，而是在构建阶段按
`DSH_REPO + DSH_TAG` 重新 clone 官方源码，然后执行：

```text
官方 tag
  → git clone --depth 1
  → pnpm install
  → pnpm run build
```

所以迁移到新机器时，不依赖旧机器上被改脏的源码目录；本项目需要保留的实现全部
位于外层 Docker、Nginx、provisioner、Compose 和文档文件中。

检查方式：

```bash
git status --short --untracked-files=all
git check-ignore -v deepseek-harness/deepseek-harness-dsh-v0.1.5-rc.2-git
git ls-files --stage -- deepseek-harness
```

`README.md` 等新文件第一次出现为 `??` 是正常的；`git diff` 默认不显示未跟踪文件，
要看完整内容用 `git status`，确认后再：

```bash
git add deepseek-harness/README.md deepseek-harness/Dockerfile \
  deepseek-harness/nginx.conf deepseek-harness/gateway-nginx.conf \
  deepseek-harness/provision.py deepseek-harness/start.sh \
  deepseek-harness/settings.yaml
git diff --cached --stat
```

不要执行：

```bash
git add deepseek-harness/deepseek-harness-dsh-v0.1.5-rc.2-git
```

如果它已经被错误地暂存成 gitlink，在确认工作区源码需要保留后，使用下面命令只从
根仓库索引移除，不删除磁盘上的官方源码：

```bash
git rm --cached -- deepseek-harness/deepseek-harness-dsh-v0.1.5-rc.2-git
```

## 2. 为什么不是直接把 DSH 绑定到 `0.0.0.0`

`dsh web --host 0.0.0.0` 在当前 `dsh-v0.1.5-rc.2` 中会被主动拒绝。
这不是 Docker 问题，而是 Harness Web 对远程代码执行能力的安全保护。

本方案不修改上游源码，而是：

1. 每个 DSH 实例只监听容器内 `127.0.0.1:<动态端口>`；
2. 容器内 Nginx 监听 `0.0.0.0:3080`；
3. 外层统一入口 Nginx 只发布宿主机原有的 `4000`；
4. 外层 `/deepseek-harness/` 代理到 DSH，其他路径继续代理到 LiteLLM；
5. DSH 内层 Nginx 完成 Basic Auth、WebSocket upgrade 和 loopback 来源处理；
6. Compose 不再发布宿主机 `3080`。

如果服务最终暴露到公网，必须再在宿主机或上层网关使用 HTTPS。纯 HTTP 下的
Basic Auth 只适合先做内网验证，不能保护公网密码。

## 3. 多用户模型

### 4.1 用户认证

认证文件位于 Docker 命名卷里的 `/data/htpasswd`，由 Nginx 的 `auth_basic` 读取。
首个用户由 Compose 环境变量创建。下面两项应写入**仓库根目录的 `.env`**，
不是写入本目录的其他配置文件：

```dotenv
DSH_BOOTSTRAP_USER=mengweiming
DSH_BOOTSTRAP_PASSWORD=请换成强密码
```

当前 `docker-compose.yml` 为兼容旧 `.env` 提供了回退：未设置时用户名为 `admin`，
密码复用 `CONSOLE_PASSWORD`。这只用于避免升级后容器因缺变量无法启动；正式部署
应显式设置上面的专用账号和密码，不建议长期与 8787 运维控制台共用密码。

增加用户：

```bash
docker compose exec deepseek-harness \
  htpasswd -B /data/htpasswd alice
```

删除用户：

```bash
docker compose exec deepseek-harness \
  htpasswd -D /data/htpasswd alice
```

### 4.2 首次访问创建专属工作区

Basic Auth 成功后，Nginx 用 `auth_request` 调用 `provision.py`。provisioner 会：

1. 校验用户名只能包含字母、数字、`.`、`_`、`-`；
2. 创建 `/workspaces/<username>`；
3. 创建 `/data/users/<username>/.dsh`；
4. 为该用户生成持久化 `settings.yaml`；
5. 在空闲端口上启动该用户的 `dsh web`；
6. 把端口通过 `X-DSH-Port` 返回给 Nginx；
7. Nginx 将当前请求和后续 WebSocket 请求转发到该用户实例。

不同用户不会共享 DSH 会话和默认工作目录。容器重启后，目录和设置仍然保留，
但用户实例会在下一次访问时重新拉起。

### 4.3 隔离边界

每个用户隔离的是 DSH 进程、`DSH_HOME` 和 workspace。当前容器仍然共享：

- 同一个 Docker 容器内核；
- 同一个 LiteLLM 后端；
- 默认相同的网关地址和模型种子清单；
- 同一个容器网络。

因此当前版本适合可信用户或内网团队，不应直接当作不可信公网用户的强隔离平台。
如果需要真正的租户级隔离，下一阶段应改成“一个用户一个容器/Pod”，并为每个用户
配置独立 LiteLLM virtual key、CPU/内存/PID 限额和独立网络策略。

## 4. LiteLLM / 模型配置

每个用户的 `settings.yaml` 使用 DeepSeek Harness 的 `llm-pi-ai` 自定义 provider：

```yaml
llm-pi-ai:
  providers:
    private-gateway:
      displayName: Private LiteLLM Gateway
      apiKeyEnv: DSH_LITELLM_API_KEY
      api: openai-completions
      baseURL: http://litellm:4000/v1
      defaultContextWindow: 128000
      defaultMaxTokens: 32768
      defaultInput: [text]
      models:
        - id: deepseek-v4.1-flash
          name: deepseek-v4.1-flash
        - id: glm-5.3-flash
          name: glm-5.3-flash
```

这里的 `id` 必须是 LiteLLM 对外暴露的模型名。`DSH_DEFAULT_MODEL_IDS` 只用于
第一次创建用户 `settings.yaml` 时生成默认模型清单；用户之后可以在网页的
**设置 → 模型**中修改提供方和模型列表。

`apiKeyEnv: DSH_LITELLM_API_KEY` 在这里是一个**凭据引用名**，不是要求把密钥写入
Compose 或根目录 `.env`。当前实现不会设置容器级 `DSH_LITELLM_API_KEY`，否则
环境变量会覆盖用户自己的凭据。

每个用户第一次进入 Harness 后，应在：

```text
设置 → 模型 → Private LiteLLM Gateway
```

中填写自己的 LiteLLM virtual key。DSH 会把密钥保存到该用户独立的：

```text
/data/users/<username>/.dsh/.credentials.yaml
```

因此推荐的用户流程是：

```text
管理员在 LiteLLM 创建 virtual key
    ↓
用户登录自己的 DeepSeek Harness
    ↓
用户在“设置 → 模型”填写自己的 key
    ↓
该 key 只保存在该用户的 DSH_HOME
```

这样可以按用户统计用量、设置额度和撤销 key，不需要把所有用户绑定到同一把
LiteLLM master key。

## 5. Nginx 配置说明

这里有两层 Nginx：

1. `nginx.conf`：DeepSeek Harness 容器内的认证和多用户 upstream；
2. `gateway-nginx.conf`：宿主机唯一的 `4000` 入口，把 `/deepseek-harness/`
   和 LiteLLM 分流。

外层入口的目标是：

```text
http://<服务器>:4000/deepseek-harness/
    ├── 页面/静态文件 → deepseek-harness:3080
    ├── /api/remote.mux → deepseek-harness:3080（WebSocket）
    ├── Referer 来自 /deepseek-harness/ 的 /api/* → deepseek-harness:3080
    └── 其他 /api/*、/v1/*、/ui/* → litellm:4000
```

之所以要处理根路径 `/api`，是因为当前 DSH Web 客户端的 RPC 和 Remote WebSocket
使用绝对路径 `/api`、`/api/remote.mux`。单纯把 `/deepseek-harness/` 反向代理到
DSH，会导致页面加载成功但 Agent 请求被发到 LiteLLM；外层 Nginx 必须根据
Harness 页面的 Referer 做一次分流。

容器内 `nginx.conf` 的核心不是普通静态代理，而是“认证后决定用户 upstream”：

```nginx
location = /__provision {
    internal;
    proxy_pass http://127.0.0.1:3090/provision;
    proxy_set_header X-Remote-User $remote_user;
}

location / {
    auth_request /__provision;
    auth_request_set $dsh_port $upstream_http_x_dsh_port;

    proxy_pass http://127.0.0.1:$dsh_port;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host 127.0.0.1;
    proxy_set_header Origin http://127.0.0.1;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

几个容易出错的点：

- `proxy_http_version 1.1` 和 `Upgrade` / `Connection` 是 WebSocket 必需的；
- `proxy_read_timeout` 必须足够大，否则长任务会被 Nginx 提前断开；
- `auth_request` 只做认证/分配，不转发业务 body；
- `proxy_pass` 使用 provisioner 返回的动态端口，不能写死一个共享 DSH；
- 不要把用户目录通过 Nginx 静态暴露；
- 不要把 `3080` 映射到 DSH 内部端口，外部只能看到 Nginx。

公网部署时仍然建议在宿主机已有反向代理上做 TLS。现在宿主机对外只有原来的
`4000`，如果已有上游 Nginx/Caddy，应把 HTTPS 流量转发到这个统一入口：

```text
Internet :443
  ↓ HTTPS
宿主机 Nginx/Caddy
  ↓ 内网 HTTP
统一入口 :4000
  ├── /v1/* → LiteLLM
  └── /deepseek-harness/ → DSH
  ↓ Basic Auth + WebSocket
用户自己的 DSH 进程
```

## 6. Compose 启动约定

根目录 `docker-compose.yml` 中的 `deepseek-harness` 服务负责构建和运行本目录：

```bash
# 第一次需要先确保上游源码 checkout 存在（用于核对版本，不参与 Docker 构建）
git clone --branch dsh-v0.1.5-rc.2 --depth 1 \
  https://github.com/deepseek-ai/deepseek-harness.git \
  deepseek-harness/deepseek-harness-dsh-v0.1.5-rc.2-git

# 在根目录构建并启动；gateway-edge 占用宿主机原有的 4000
docker compose build deepseek-harness
docker compose up -d deepseek-harness gateway-edge
docker compose logs -f deepseek-harness gateway-edge
```

Compose 持久化两个命名卷：

```text
deepseek_harness_data        → /data
deepseek_harness_workspaces  → /workspaces
```

不要执行 `docker compose down -v`，否则所有用户的 Harness 会话、认证文件和工作区
都会被删除。

## 7. 安全边界

禁止使用：

```text
--privileged
--network host
-v /:/host
-v /var/run/docker.sock:/var/run/docker.sock
```

同时建议：

- Compose 使用 `no-new-privileges`；
- 设置 `pids_limit` 和内存上限；
- 只发布统一入口 `4000`，不要发布 DSH 容器内的 `3080`；
- DSH 用户只获得自己的 `/workspaces/<username>`；
- 备份 `/data` 前先限制备份文件权限；
- 对公网使用 HTTPS；
- 不把用户的 LiteLLM virtual key 写进源码、Compose 或 README；
- 上游源码 checkout、`node_modules`、运行时数据不提交根仓库。

## 8. 开发顺序

当前实现按以下顺序推进：

1. **Git 边界**：忽略上游嵌套 checkout，只跟踪部署封装；
2. **单用户**：验证 Docker build、Nginx、WebSocket 和 LiteLLM 调用；
3. **多用户进程路由**：验证首次登录建目录、独立 `DSH_HOME` 和端口路由；
4. **模型能力**：补充 LiteLLM 模型清单、reasoning 和图片输入声明；
5. **生产化**：外层 HTTPS、独立 virtual key、限额、审计和每用户容器隔离。

## 9. 验证清单

```bash
docker compose config
docker compose build deepseek-harness
docker compose up -d deepseek-harness gateway-edge
docker compose ps

# LiteLLM 和 Harness 共用原来的 4000
curl -i http://127.0.0.1:4000/deepseek-harness/

# 认证后访问
curl -u mengweiming:'密码' -i http://127.0.0.1:4000/deepseek-harness/

# 第一次访问后查看用户目录
docker compose exec deepseek-harness \
  find /data/users /workspaces -maxdepth 2 -type d
```

浏览器验证时要实际完成：

1. 选择一个 LiteLLM 模型并发一条普通对话；
2. 让 Agent 在自己的 workspace 创建文件；
3. 用另一个用户登录，确认看不到前一个用户的会话和 workspace；
4. 重启容器后再次登录，确认会话和文件仍然存在。

## 10. 当前限制

- Basic Auth 不是完整账号系统，没有找回密码、组织、角色和审计页面；
- 用户必须由管理员预先加入 `htpasswd`，首次请求才创建 workspace；
- 所有用户默认共享一把 LiteLLM key；
- 一个用户对应一个长期运行的 DSH 进程，用户多时要提高内存和 PID 限额；
- 当前版本的 DSH/插件协议仍可能变化，升级版本必须重新执行完整验证；
- 上游源码目录仅用于本地参考，不属于根仓库的业务代码。
