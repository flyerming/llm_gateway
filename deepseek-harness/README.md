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
├── nginx.conf                             # 本项目跟踪（DSH 容器内层）
├── gateway-nginx.conf                     # 本项目跟踪（宿主机 4000 统一入口）
├── provision.py                           # 本项目跟踪
├── start.sh                               # 本项目跟踪
├── settings.yaml                          # 本项目跟踪
├── workspace-delete-cleanup.mjs            # 本项目跟踪（删除工作区时清理目录）
├── cordis.patch.yml                       # 本项目跟踪（home 级补丁模板）
└── deepseek-harness-dsh-v0.1.5-rc.2-git/  # 本地 checkout，不提交、不修改
```

上游 checkout 自带 `.git`。如果把它直接 `git add` 到根项目，根 Git 会把它记录成
`160000` 的嵌套仓库/gitlink，而不会跟踪里面的普通文件；这不是 Git 失效。
根目录 `.gitignore` 现在明确忽略该 checkout、依赖包以及运行时数据。

本项目的 Dockerfile **直接复制本地上游 checkout**：构建阶段不再访问 GitHub，
而是用相对路径 `COPY` 把源码放进镜像，然后执行：

```text
deepseek-harness/deepseek-harness-dsh-<tag>-git/     （相对路径）
  → COPY ${DSH_SRC_DIR} /opt/dsh
  → pnpm install
  → pnpm run build
```

之所以改成复制而不是构建期 `git clone`，是因为构建容器常常拿不到 GitHub 的网络
路径（国内直连超时、企业代理不覆盖 build 容器、registry 与 GitHub 是两条出口），
而源码本来就有一份官方原始下载的 checkout 在本地。复制本地源码可以：

- 让 `docker compose build` 不再依赖 GitHub 可达性；
- 让构建结果只取决于源码目录内容，可复现、可离线重跑；
- 迁移到新机器时，只需把官方下载的源码放到同一**相对路径**下。

相关变量：

```dotenv
# 相对 deepseek-harness/ 构建上下文
DSH_SRC_DIR=deepseek-harness-dsh-v0.1.5-rc.2-git
# 该目录对应官方 tag 的 commit（源码没有 .git，用它补齐构建元数据）
DSH_COMMIT_HASH=fb2c4b9e698e30edb738bca4cf0618587db7d203
```

两点必须记住：

1. **源码目录必须是 Linux 上从官方仓库克隆/下载的原始内容。** 在 Windows 上
   `git clone` 默认 `core.symlinks=false`，会把源码里的符号链接摊平成普通文件，
   复制进镜像后行为就和官方不一致；Linux 上克隆则没有这个问题。
2. **不要复制被修改过的源码目录。** Dockerfile 复制的是构建上下文里的原始
   checkout，本项目对 DSH 的定制全部在容器外层的 Nginx / provisioner / Compose
   文件里，不需要改上游源码。

本项目需要保留的实现全部位于外层 Docker、Nginx、provisioner、Compose 和文档文件中。

`cordis.patch.yml` 是外部可维护的 home 级补丁模板。镜像构建时它不是
Dockerfile 的硬性 `COPY` 输入：`provision.py` 在镜像内置了同一份兜底内容，
如果 `/etc/dsh/cordis.patch.yml` 存在就优先读取外部文件。这样从旧 Git 工作树
或不完整压缩包迁移时，即使漏掉这个新文件，镜像也不会在 Dockerfile 的 `COPY`
阶段失败，联网搜索与模型配置修复仍会生效。正常维护时仍建议把
`deepseek-harness/cordis.patch.yml` 纳入根项目 Git 跟踪，便于后续修改补丁。

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
  deepseek-harness/settings.yaml deepseek-harness/cordis.patch.yml
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

### 3.1 用户认证

认证文件不在用户数据卷里，而是放在**独立命名卷**挂载的
`/etc/dsh-auth/htpasswd`（目录 `0750 root:www-data`，文件 `0640 root:www-data`），
由 Nginx 的 `auth_basic` 读取。这么放有两个原因：

- 容器里的 Agent 以各自的专属 UID 降权运行（见 §3.3），既列举不到
  `/etc/dsh-auth`，也不在 `www-data` 组里，所以读不到密码摘要；
- `/data` 将来要整体备份或移交给别人，密码摘要不该跟着走。

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
  htpasswd -B /etc/dsh-auth/htpasswd alice
```

删除用户：

```bash
docker compose exec deepseek-harness \
  htpasswd -D /etc/dsh-auth/htpasswd alice
```

> 手动 `htpasswd` 写入后文件权限会变回 `0600 root:root`，Nginx worker 就再也
> 读不到它了（症状：不输密码 401，输对密码反而 500）。写完补一条
> `chown root:www-data /etc/dsh-auth/htpasswd && chmod 0640 /etc/dsh-auth/htpasswd`
> 即可，或者直接 `docker compose restart deepseek-harness` —— `start.sh` 每次
> 启动都会把权限修回来。

历史版本的 `/data/htpasswd`、`/data/.uidmap` 会在启动时自动迁移到
`/etc/dsh-auth/`，迁移后旧文件被删除；旧目录不想保留可以手工删掉。

### 3.2 首次访问创建专属工作区

Basic Auth 成功后，Nginx 用 `auth_request` 调用 `provision.py`。provisioner 会：

1. 校验用户名：首字符必须是字母/数字/下划线，其余只允许字母、数字、`.`、`_`、`-`，
   所以 `.`、`..`、隐藏目录名这类能穿越路径的名字一律被拒绝；
2. 从持久映射里取（或首次分配）该用户的专属 UID；
3. 创建 `/workspaces/<username>`；
4. 创建 `/data/users/<username>/.dsh`；
5. 把这两棵目录树 `chown` 给该 UID、并收紧到 `0700`；
6. 为该用户生成持久化 `settings.yaml`；
7. 以该 UID 降权启动该用户的 `dsh web`；
8. 把端口通过 `X-DSH-Port` 返回给 Nginx；
9. Nginx 将当前请求和后续 WebSocket 请求转发到该用户实例。

不同用户不会共享 DSH 会话和默认工作目录。容器重启后，目录、设置和 UID 映射仍然
保留，但用户实例会在下一次访问时重新拉起。

> **默认工作目录**：`dsh web` 以 `cwd=/workspaces/<username>` 启动，所以新建
> 会话的默认 workspace 就是该用户自己的 `/workspaces/<username>`；官方
> `packages/api/session-controller` 与 `sandbox-policy.config.workspaceRoot`
> 都读 `process.cwd()`，因此这里必须是用户自己的目录，不能是只读的安装树
> `/opt/dsh`。`/data/users/<username>` 只放 Harness 自己的状态（`$DSH_HOME`：
> `settings.yaml`、`cordis.patch.yml`、`sessions/`、`.credentials.yaml`、日志、
> 私有 `TMPDIR`）。
>
> 历史版本（以 `cwd=/opt/dsh` 启动）遗留的会话可能把 workspace 记成
> `/data/users/<user>/test` 这类手工创建的路径。这不影响隔离（那些目录同样
> `0700` 且属该用户），只是口径不同；新建会话即回到 `/workspaces/<username>`。
> Web UI 里也可以自行注册别的目录，但只能注册自己的目录，注册到别人的目录会
> 因 `0700` 权限直接失败。
>
> ⚠️ 因为 cwd 变成了用户可写的目录，该目录下的 `.env` 会被 DSH 当作「项目层」
> 读取。若里面出现 `DSH_`/`HTTP_PROXY` 这类启动期变量，DSH 会拒绝启动（这是
> 刻意的防提权设计），症状是该用户访问时 500、日志报
> `sets "..." which only the launching environment may set`。删掉
> `/workspaces/<user>/.env` 即可恢复；只影响该用户自己的实例。

> **删除工作区的部署语义**：官方 DSH 的“删除工作区”默认只删除注册关系，
> 保留文件夹和会话日志；本部署额外加载 `workspace-delete-cleanup.mjs`，
> 因此浏览器确认删除后会同步递归删除该工作区目录。清理插件只允许删除当前
> 用户 HOME 或 `/workspaces/<user>` 下的子目录，并拒绝用户根目录、`.dsh`、
> `tmp` 等保留路径。删除失败会写入 Harness 日志，不会伪装成删除成功后再越权
> 删除其他路径；请在点击确认前把需要保留的文件移出工作区。

### 3.3 隔离边界

过去所有用户的 `dsh web` 都以 **root** 运行，chmod 形同虚设（root 无视权限位），
于是任意用户的 Agent 都能读 `/data/htpasswd`，也能读写别人的
`/data/users/<other>`、`/workspaces/<other>`。现在改成 **每个用户一个容器内
UID**：

```text
用户 → UID 映射（/etc/dsh-auth/.uidmap，0600 root）
mengweiming → 20000
alice       → 20001
...
```

- UID 从 `DSH_BASE_UID`（默认 20000）起顺序分配，上限 `DSH_UID_CEILING`
  （默认 60000）；映射写在认证卷里，所以容器重建后老文件仍归同一个 UID 所有。
- provisioner 用 `subprocess` 的 `user=`/`group=`/`extra_groups=[]` 启动
  `dsh web`：真正 `setgid` + `setuid`，并清空 root 的附加组（否则还会留在
  `www-data` 组里、照样能读 htpasswd）。
- 用户进程的 `umask` 固定 `077`，新建文件默认 `0600`、目录默认 `0700`。
- `/data`、`/data/users`、`/workspaces` 一律 `0711`（**可穿越、不可列举**）：
  用户知道自己目录的完整路径，但 `ls /data`、`ls /data/users` 都列不出别人。
- 每个用户目录 `0700` 且属该用户自己的 UID，因此互相不可读写。
- 每个用户一个私有 `TMPDIR`（`$HOME/tmp`），不共用 `/tmp`，也没有
  `/tmp/dsh-<user>` 这种可以被别人预置符号链接的路径。
- provisioner 自己仍在 root（它要 `chown`/`setuid`），所以在写用户目录里的文件
  前一律先 `lstat` + `O_NOFOLLOW`：用户即便把 `dsh.log` 或 `settings.yaml`
  换成指向 `/etc/passwd` 的符号链接，也只会被当成异常文件删掉，不会被 root
  跟着写穿。
- 为了让 `git commit`、`whoami` 这类工具能解析「当前用户」，provisioner 会为
  每个 UID 在 `/etc/passwd`、`/etc/group` 里补一行 `dsh-<username>` 占位条目
  （这两个文件不进数据卷，容器重建后由 provisioner 自动补回）。

改动这些行为前请先读 `provision.py` 顶部 `BASE_UID` / `ISOLATE_USERS` 的注释。
**逃生开关**：`DSH_ISOLATE_USERS=0` 会退回「所有用户共用一个 root 进程」的旧
行为，此时上面这些隔离全部失效，只能用来临时排障。

> 注意：这套 UID 隔离保证的是**用户之间**的边界。若把沙箱模式设成
> `danger-full-access`（本机 AlmaLinux 8 上为了避免每次命令都弹审批，见 §9.5），
> 单个用户自己的命令就不再受文件沙箱约束 —— 那属于单用户内部的纵深防御，
> 与用户间隔离是两回事。

仍然共享、**尚未隔离**的部分：

- 同一个 Docker 容器内核（同一 PID/网络/挂载命名空间）；
- 同一个 LiteLLM 后端；
- 默认相同的网关地址和模型种子清单；
- 同一个容器网络。

因此「每用户 UID」解决的是**文件互相可见/可篡改**和**读到 htpasswd**这两类问题，
它不等于强隔离：容器内核共享，`pids_limit`/内存是整容器级别的，恶意用户理论上仍
可能通过内核漏洞或共享的服务端点影响别人。要真正的租户级隔离，下一阶段应改成
「一个用户一个容器/Pod」，并为每个用户配独立 LiteLLM virtual key、CPU/内存/PID
限额和独立网络策略。

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

#### 4.1 覆盖内置默认模型（否则会话报 `deepseek-official` 缺少凭据）

DSH 官方内置的默认模型走它自己的 `deepseek-official` provider（见官方
`packages/bundle/base/cordis.patch.yml` 里的 `agent-default-model`），该 provider
只认环境变量 `DEEPSEEK_API_KEY`。我们的部署不会导出这个变量，所以如果不在用户
设置层覆盖它，新建会话发出的第一句话就会失败：

```text
本轮运行失败 llm-deepseek: no API key for provider route "deepseek-official",
store DEEPSEEK_API_KEY through the credentials service ... (MISSING_CREDENTIAL)
```

因此模板开头会写入一个 `agent-default-model` 段，把默认模型改指私有网关：

```yaml
agent-default-model:
  provider: private-gateway
  model: __DSH_DEFAULT_MODEL__      # provision.py 填成 DSH_DEFAULT_MODEL_IDS 的第一个模型
```

DSH 的用户设置文档层优先级高于组件内置的 `base`（官方
`packages/settings/settings/src/index.ts` 里注明的合并顺序是
“schema 默认值 → 注册方的 composition `base` → 用户文档段”），所以这个段能覆盖
官方默认值。`provider` 必须与上面 `llm-pi-ai.providers` 里的路由名一致
（这里是 `private-gateway`）。

#### 4.2 模板占位符约定

模板里有**两个**占位符，各自在整个文件中必须**只出现一次**，且都不能出现在注释
里：

- `__DSH_MODELS__`：模型清单；
- `__DSH_DEFAULT_MODEL__`：`agent-default-model.model`。

> ⚠️ `provision.py` 用 `str.replace` 渲染模板，早期版本在注释里也写了一遍
> `__DSH_MODELS__`，结果模型块被塞进注释中间，渲染出非法 YAML，`dsh web`
> 启动即崩：
> `settings-file: invalid document ... UNEXPECTED_TOKEN`（`error_code 12`）。
> 用户 `settings.yaml` 存在数据卷里，重建镜像不会覆盖，因此 `provision.py`
> 会检测坏文件特征串（`with DSH_DEFAULT_MODEL_IDS.`）并在启动时自动重新生成
> （自愈）。同理，缺少 `agent-default-model:` 段的旧文件也会被重新渲染，避免
> 用户一直卡在 `deepseek-official` 缺凭据上。改动模板时务必保持“注释里不出现
> 占位符”。

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

#### 4.3 思考强度（reasoning effort）为什么可选

手写的模型条目默认视为「无推理能力」，DSH 因而不会渲染思考强度选择器 —— 这正是
早期版本选了私有网关模型后「不能选思考强度」的原因。修法是给每个模型补上
`reasoningEfforts`（可选档位 → 发给网关的线值），并用 `compat.thinkingFormat`
声明线协议；`provision.py` 的 `model_block()` 现在会为 `DSH_DEFAULT_MODEL_IDS`
里的每个模型自动生成：

```yaml
        - id: deepseek-v4.1-flash
          name: deepseek-v4.1-flash
          compat:
            supportsReasoningEffort: true
            thinkingFormat: openai     # 由 DSH_MODEL_REASONING_FORMAT 决定
          reasoningEfforts:
            off: null                  # 只有 off 允许留空（该档不发送线值）
            low: low
            high: high
            max: max
```

规则（见官方 `packages/llm/llm-pi-ai/src/catalog.ts` 的 `resolveModelReasoning`）：

- 当前私有 DeepSeek 路由对齐官方模型，界面可选档位是
  `off`/`low`/`high`/`max`；
  value 是实际发给网关的线值；只有 `off` 允许为空（`null`）；
- 至少要声明一个非 `off` 档位，否则 DSH 视为配置错误；
- 未声明的档位会被 pin 成「不可选」。因此想少几个档位，去掉对应行即可。

`compat.thinkingFormat` 的默认值是 `openai`（LiteLLM 以 OpenAI 兼容方式转发
`reasoning_effort` 时最通用）：如需改成 DeepSeek 风格，在 `.env` 里设
`DSH_MODEL_REASONING_FORMAT=deepseek` 后重建/重启 `deepseek-harness`。可选值与
官方 `SUPPORTED_THINKING_FORMATS` 一致（`openai`/`deepseek`/`qwen`/`zai`/
`openrouter`/`together`/`baseten`/`chat-template`/…）。

#### 4.4 联网搜索：改用 searxng MCP，绕过内置 deepseek-official

DSH 内置的 `web_search` 走它自己的 `deepseek-official` provider，只认环境变量
`DEEPSEEK_API_KEY`；本部署不导出该变量，所以一搜索就报缺少 key。修复不动官方
源码，而是通过 **home 级补丁层**（`$DSH_HOME/cordis.patch.yml`，DSH 的 compose
顺序里它排在官方 bundle/profile 之后、`--patch` overlay 之前）做两件事：

1. 把内置 `web.searchProvider` 指到一个**未注册**的 id（`none`），让内置
   `web_search` 在调用时确定性地失败（`WEB_PROVIDER_CONFIGURED_MISSING`），
   模型收到明确错误后改走 MCP 搜索工具；`fetchProvider` 仍指向内置 `http`
   provider，`web_fetch` 不需要任何 key，保持可用；
2. 用 `@deepseek-ai/dsh-mcp-client` 接入 compose 里的 `searxng-mcp:8090`，
   模型从此获得 `mcp__searxng__searxng_web_search` /
   `mcp__searxng__web_url_read` / `mcp__searxng__searxng_instance_info` 等工具
   （DSH 直连 8090）。工具名是 mcp-client 统一生成的
   `mcp__<serverName>__<rawName>`，本部署 `serverName: searxng`，所以每个名字都
   带 `mcp__searxng__` 前缀；模型要调的就是这些全名。

补丁模板是 `deepseek-harness/cordis.patch.yml`，构建时 `COPY` 到
`/etc/dsh/cordis.patch.yml`；`provision.py` 在每个用户首次访问时把它写进
`/data/users/<user>/.dsh/cordis.patch.yml`，并像 `settings.yaml` 一样做陈旧自愈
（特征串 `mcp-searxng` 缺失就重写），所以模板升级能自动推到已有用户。

MCP 的 Bearer token 不写进补丁文件，而是由 `provision.py` 通过用户进程环境变量
`MCP_SEARXNG_TOKEN` 注入，补丁里用 `!!js '`Bearer ${process.env.MCP_SEARXNG_TOKEN}`'`
在 Loader 求值时拼进请求头。这个 token 来自根目录 `.env` 的 `MCP_SEARXNG_TOKEN`
（与 codex/Claude Code 客户端共用），属于**服务端内部凭据**，不是每个用户自己的
模型 key；`searxng-mcp` 的 8090 端口只在 compose 网络内暴露，不对宿主发布。

`deepseek-harness` 服务因此在 `depends_on` 中加入了 `searxng-mcp`，并在
`environment` 中声明了 `MCP_SEARXNG_TOKEN`。

## 5. Nginx 配置说明

这里有两层 Nginx：

1. `nginx.conf`：DeepSeek Harness 容器内的认证和多用户 upstream；
2. `gateway-nginx.conf`：宿主机唯一的 `4000` 入口，把 `/deepseek-harness/`
   和 LiteLLM 分流。

外层入口的目标是：

```text
http://<服务器>:4000/deepseek-harness/
    ├── 页面/静态文件 → deepseek-harness:3080
    ├── /plugins/*（客户端插件 bundle + HMR SSE）→ deepseek-harness:3080
    ├── /api/remote.mux → deepseek-harness:3080（WebSocket）
    ├── Referer 来自 /deepseek-harness/ 的 /api/* → deepseek-harness:3080
    └── 其他 /api/*、/v1/*、/ui/* → litellm:4000
```

之所以要处理根路径 `/api`，是因为当前 DSH Web 客户端的 RPC 和 Remote WebSocket
使用绝对路径 `/api`、`/api/remote.mux`。单纯把 `/deepseek-harness/` 反向代理到
DSH，会导致页面加载成功但 Agent 请求被发到 LiteLLM；外层 Nginx 必须根据
Harness 页面的 Referer 做一次分流。

子路径本身可用，靠的是 Vite 的 `base: './'`（见官方 `apps/web/vite.config.ts`）：
构建产物 `dist/index.html` 里资源都是相对路径（`./assets/...`），页面落在
`/deepseek-harness/` 下时自然解析到该前缀下，再被外层 `^~ /deepseek-harness/`
剥掉前缀转发。外层那条 `sub_filter '<base href="/">' ...` 因此是**防御性空操作**
（当前产物没有 `<base href>` 标签），只在将来构建改回绝对 `<base href="/">` 时才
有意义。

`/plugins/` 则**不能靠 `base href` 解决**：客户端插件的
bundle 和 HMR 事件端点由 DSH 直接生成**绝对路径**（见官方
`packages/client/modules/src/index.ts` 的 `/plugins/??<ids>&rev=...` 与
`/plugins/events`），必须在外层单独开一个 `location ^~ /plugins/` 转发到 DSH。

#### 5.1 为什么还要注入 `__DSH_TRANSPORT__`

DSH 只在「页面位于本机回环地址」时才启用设置持久化。官方
`packages/client/ui-settings/src/client/index.ts`：

```ts
const persistence = ctx.remote.$host.isLoopback ? 'host' : 'memory'
```

而 `isLoopback`（`packages/client/connection/src/loopback-hostname.ts`）只认
`localhost`、`127.0.0.1`、`[::1]`。我们通过反向代理用
`http://10.18.219.156:4000/deepseek-harness/` 访问，hostname 是 `10.x`，于是
设置被判定为 `memory` 模式 → 设置镜像永远停在 `unavailable`，网页会报：

```text
加载提供方目录失败: settings are unavailable in this browser
```

表现为「页面能打开，但模型设置页打不开」，也就没法在 UI 里填 LiteLLM key。

外层 Nginx 在内联注一段 transport hooks 来绕过这个判断：

```nginx
# 必须插在入口 <script type="module"> 之前：模块脚本是 defer 的，
# 内联经典脚本先执行，先设置的全局变量才能被后续模块读到。
sub_filter '<script type="module" crossorigin src=' \
           '<script>globalThis.__DSH_TRANSPORT__={ownsHost:true}</script><script type="module" crossorigin src=';
```

只补 `ownsHost`、不补 `fetch` 是安全的：连接层
（`packages/client/connection/src/client/index.ts`）在没有自定义 `fetch` 时会退回
`globalThis.fetch`，其余 hook 均为可选。服务端另有一道
`api-request-trust.ts` 的校验，要求 Host 是回环或在 `trustedHosts` 里；内层
`nginx.conf` 已经把 `Host` / `Origin` 固定成 `127.0.0.1`，满足该条件。

> 回退方式：如果某个 DSH 版本不再认 `__DSH_TRANSPORT__` 这个全局变量，删掉
> 这一条 `sub_filter` 即可；届时若还要用设置页，只能把页面放到
> `localhost`/`127.0.0.1` 主机名下访问（例如做 SSH 端口转发或本机 hosts 映射）。

外层还有一个容易漏掉的坑：`/api` 的分流用的是**变量形式**的
`proxy_pass $api_backend`。nginx 对变量里的主机名只能在**运行期**解析，这时它
不会退回启动期解析，而是直接报 `no resolver defined to resolve "litellm"` 并回
502。所以 `http {}` 里必须有 `resolver`：

```nginx
# 127.0.0.11 是 Docker 内置 DNS，compose 网络里的服务名都靠它解析。
# valid 取小值：容器重建后 IP 会变，长 TTL 会把请求打到已消失的旧地址。
resolver 127.0.0.11 valid=10s ipv6=off;
```

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
    auth_request_set $dsh_cookie $upstream_http_x_dsh_cookie;

    proxy_pass http://127.0.0.1:$dsh_port;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host 127.0.0.1;
    proxy_set_header Origin http://127.0.0.1;
    proxy_set_header Cookie $dsh_cookie;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

几个容易出错的点：

- `proxy_http_version 1.1` 和 `Upgrade` / `Connection` 是 WebSocket 必需的；
- `proxy_read_timeout` 必须足够大，否则长任务会被 Nginx 提前断开；
- `auth_request` 只做认证/分配，不转发业务 body；
- `location = /__provision` 上单独放宽了 `proxy_read_timeout 300s`：provisioner
  现在是「等用户实例真正可用才返回」（首次访问要等 `dsh web` 起完并兑换会话
  cookie）。auth_request 子请求默认 60s 读超时，不放宽就会出现“冷启动第一次
  访问 500、刷新一下就好”的假故障；
- `proxy_pass` 使用 provisioner 返回的动态端口，不能写死一个共享 DSH；
- `Host` / `Origin` / `Cookie` 三者必须成套：DSH 的会话 cookie 是**按请求
  authority 签名**的，内层固定 `Host: 127.0.0.1`，所以 cookie 也必须按这个
  authority 生成、并在后续每个请求里按同一个值注入（详见 §9.2）；
- **两层** Nginx 都要设 `client_max_body_size`：默认只有 `1m`，而 DSH 的
  Agent 可以上传附件，外层还要转发 Codex / Claude Code 的大请求体。两层都
  放宽到 `512m`（见 §9.4 的 413 排障）；
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
# 第一次需要先确保上游源码 checkout 存在（会被 COPY 进 Docker 镜像）
git clone --branch dsh-v0.1.5-rc.2 --depth 1 \
  https://github.com/deepseek-ai/deepseek-harness.git \
  deepseek-harness/deepseek-harness-dsh-v0.1.5-rc.2-git

# 在根目录构建并启动；gateway-edge 占用宿主机原有的 4000
docker compose build deepseek-harness
docker compose up -d deepseek-harness gateway-edge
docker compose logs -f deepseek-harness gateway-edge
```

上面这条 `git clone` 只需要在**宿主机上**能跑通一次（也可以在别处下载后拷过来）。
源码就位后，Docker 构建阶段不再访问 GitHub。

Compose 持久化三个命名卷：

```text
dsh_data        → /data
dsh_workspaces  → /workspaces
dsh_auth        → /etc/dsh-auth（htpasswd + UID 映射）
```

（compose 会加上项目名前缀，实际卷名形如 `litellm-0914-deploy_dsh_data`。）

认证卷刻意与数据卷分开：单独备份/移交 `/data` 时不会带走密码摘要；反过来轮换密码
也不影响用户数据。

不要执行 `docker compose down -v`，否则所有用户的 Harness 会话、认证资料和工作区
都会被删除。

顺带说明：`proxy` 服务的启动脚本里有 `for f in ...; do ... "$f" ...; done`。
在 compose 里必须写成 `$$f`，因为单个 `$` 会被 compose 当成它自己的变量去插值，
表现为每条 `docker compose` 命令都先打两行
`WARN The "f" variable is not set. Defaulting to a blank string.`（两个 build
上下文各插值一次）。这个警告和 harness 无关，但容易被误认为是 harness 报错，
已改成 `$$f` 消除。

### 6.1 构建依赖下载（npm registry 镜像）

Docker 构建时主要有两处需要访问公网：

1. 基础系统的 `apt-get update` / `apt-get install`（默认 Debian 官方源）；
2. `corepack prepare pnpm` + `pnpm install`（默认 `registry.npmjs.org`）。

源码来自本地 `COPY`（见第 1 节），不再在构建阶段访问 GitHub。

如果日志同时出现：

```text
E: Unable to locate package ...
Package 'ca-certificates' has no installation candidate
```

不要逐个替换包名。这通常表示 `apt-get update` 没有获得有效的 Debian
Packages 索引。当前 Dockerfile 已让 APT 使用构建代理、自动重试，并在索引下载
失败时立即停止，避免把网络故障伪装成“所有包都不存在”。

在国内服务器上直连 `registry.npmjs.org` 常见失败现象是构建日志里出现：

```text
[WARN] GET https://registry.npmjs.org/xxx error (23). Will retry in 1 minute. 1 retries left.
```

`error (23)` 是 libcurl 的本地写入失败：响应内容无法写入构建容器的文件系统，
也可能是中间代理在大 tarball 传输时中断。它不等同于 HTTP 404，也不是代码或
lockfile 本身的问题。

### 6.2 真正的根因：构建容器和宿主机走的是两条网络路径

换镜像源后如果日志变成 `registry.npmmirror.com` 仍然 `error (23)`，就要怀疑
**不是「哪个源」的问题，而是构建容器根本没走在能出网的那条路径上**。

判断信号非常明确：

```text
# 宿主机上直接执行，成功
$ pnpm install
# 同样的依赖，在 docker build 里失败
[WARN] GET https://registry.npmmirror.com/xxx error (23)
```

原因是 `docker build` 默认给构建容器单独分配 DNS、路由和 MTU，和宿主机的出网路径
不一致。如果宿主机是靠 mihomo / 公司代理 / 特殊 DNS 出网的，构建容器拿不到同一套
环境，于是「宿主机能下、容器下不下来」。

解决方式：让构建阶段直接复用宿主机网络栈。`docker-compose.yml` 里已经加好：

```yaml
deepseek-harness:
  build:
    network: host      # 构建期与宿主机同一条网络路径
```

这样构建期和宿主机完全一致，包括宿主机 `127.0.0.1:7890` 上的 mihomo。

### 6.3 registry 镜像与重试（辅助手段）

在正确网络路径的基础上，再做以下加固（无需改动 lockfile，也不修改上游源码）：

- `Dockerfile` 新增构建参数 `NPM_REGISTRY`，默认 `https://registry.npmmirror.com`；
- 同一值写入 `COREPACK_NPM_REGISTRY`，让 corepack 下载 pnpm 自身也走镜像；
- 写入 `/root/.npmrc`：`registry`、`network-concurrency`（默认降到 4，降低大批量
  并发 tarball 被中间设备 RST 的概率）、`child-concurrency` 和放宽后的
  `fetch-retries=5` / `fetch-timeout=600000`；
- 源码改为本地 `COPY`，构建阶段不再有 `git clone`（原先的 clone 重试已不再需要）；
- `pnpm install` 拆成 `pnpm fetch`（带重试，已下载的包保留）+ `pnpm install --offline`
  （从本地 store 链接，不再访问网络），网络抽风时重跑只补缺失部分。

需要覆盖时，在根目录 `.env` 里设置：

```dotenv
# 国内默认镜像
NPM_REGISTRY=https://registry.npmmirror.com
# 能直连 npmjs 时
# NPM_REGISTRY=https://registry.npmjs.org
# 使用私有源时
# NPM_REGISTRY=http://nexus.internal:8081/repository/npm-public/
# 并发下载连接数
NPM_FETCH_CONCURRENCY=4
```

### 6.4 宿主机自己也要靠代理出网时

`network: host` 只保证「路径一致」，如果宿主机本身就必须经过代理才能抵达 npm 源，
还要把代理地址显式传给构建。docker 的 build **不会**继承宿主机的 `*_proxy`，必须
显式声明。已在 `.env_example` 里预留：

```dotenv
# 配合 network: host，构建容器视角的 127.0.0.1 就是宿主机
DSH_BUILD_HTTP_PROXY=http://127.0.0.1:7890
DSH_BUILD_HTTPS_PROXY=http://127.0.0.1:7890
```

`docker-compose.yml` 把这两个值映射成 Dockerfile 的 `HTTP_PROXY` / `HTTPS_PROXY`
构建参数（`ARG`），在构建阶段对 `pnpm` / `corepack` / `git` / `curl` 生效，并且
**不会烧进最终镜像**，运行期容器不受影响。

APT 也使用同一组构建代理。如果 Debian 官方源在服务器网络中不可达，可以在
根目录 `.env` 成对设置镜像源：

```dotenv
DSH_APT_MIRROR=http://mirrors.aliyun.com/debian
DSH_APT_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
```

也可以只设置代理而继续使用 Debian 官方源：

```dotenv
DSH_BUILD_HTTP_PROXY=http://127.0.0.1:7890
DSH_BUILD_HTTPS_PROXY=http://127.0.0.1:7890
```

`network: host` 下，`127.0.0.1:7890` 指的是构建宿主机上的代理监听地址；
如果代理只监听 Docker Compose 的 `proxy` 服务名，则不能直接用于
`network: host` 的构建阶段。

### 6.5 `pnpm` 下载出现 `error (23)`

先检查宿主机 Docker 数据目录的空间和 inode；`curl (23)` 优先排查本地写入层：

```bash
df -h /var/lib/docker
df -i /var/lib/docker
docker system df
```

如果磁盘或 inode 接近耗尽，先清理确认不再需要的构建缓存，再重试。不要直接执行
`docker system prune -a`，它可能删除仍要使用的镜像和缓存。

如果空间正常，把并发降到 1：

```dotenv
NPM_FETCH_CONCURRENCY=1
```

如果 `registry.npmmirror.com` 对较大的二进制 tarball 仍然失败，可在代理可达时切换
到官方源：

```dotenv
NPM_REGISTRY=https://registry.npmjs.org
```

也可以在宿主机先单独验证失败 URL 是否能完整写入磁盘：

```bash
curl -fL --retry 5 \
  -o /tmp/codex.tgz \
  'https://registry.npmmirror.com/@openai/codex/-/codex-0.153.4-linux-x64.tgz'
ls -lh /tmp/codex.tgz
rm -f /tmp/codex.tgz
```

注意：`NPM_REGISTRY` 只影响 **镜像构建阶段** 拉取依赖，不参与运行期。运行期的 Harness
模型请求仍然走容器内 `http://litellm:4000/v1`。

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
- DSH 用户只获得自己的 `/workspaces/<username>`（专属 UID + 0700，见 §3.3）；
- 若因内核不支持沙箱而切到 `danger-full-access`，用户间隔离仍由专属 UID 维持，
  但单用户文件沙箱关闭（见 §9.5）；
- 备份 `/data` 前先限制备份文件权限；
- 认证卷 `/etc/dsh-auth` 与 `/data` 分开备份、分开授权，不要混在一起；
- 对公网使用 HTTPS；
- 不把用户的 LiteLLM virtual key 写进源码、Compose 或 README；
- 上游源码 checkout、`node_modules`、运行时数据不提交根仓库。

## 8. 开发顺序

当前实现按以下顺序推进：

1. **Git 边界**：忽略上游嵌套 checkout，只跟踪部署封装；
2. **单用户**：验证 Docker build、Nginx、WebSocket 和 LiteLLM 调用；
3. **多用户进程路由**：验证首次登录建目录、独立 `DSH_HOME` 和端口路由；
4. **模型能力**：补充 LiteLLM 模型清单、思考强度（`reasoningEfforts`）和图片输入声明；
5. **联网搜索**：home 级补丁关掉内置 `deepseek-official` 搜索，改接 searxng MCP；
6. **生产化**：外层 HTTPS、独立 virtual key、限额、审计和每用户容器隔离。

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

# 每个用户目录应属于自己专属 UID、权限 0700
docker compose exec deepseek-harness \
  sh -c 'ls -ld /data /data/users /workspaces /etc/dsh-auth; \
         cat /etc/dsh-auth/.uidmap; \
         ls -ln /data/users /workspaces'
```

浏览器验证时要实际完成：

1. 选择一个 LiteLLM 模型并发一条普通对话；
2. 让 Agent 在自己的 workspace 创建文件；
3. 用另一个用户登录，确认看不到前一个用户的会话和 workspace；
4. 在 Harness 里让 Agent 执行 `ls /data`、`ls /data/users`、`cat /etc/dsh-auth/htpasswd`，
   确认前两条列不出别的用户、第三条是 Permission denied；
5. 用另一个用户登录，确认 `/data/users/<other>`、`/workspaces/<other>` 都读不到（Permission denied）；
6. 重启容器后再次登录，确认会话和文件仍然存在（说明 UID 映射落盘生效）；
7. 确认模型选择器上出现「思考强度」档位（`reasoningEfforts` 生效），发一条带思考的对话；
8. 让 Agent 联网搜索一次，确认走的是 searxng MCP 工具
   （`mcp__searxng__searxng_web_search` / `mcp__searxng__web_url_read`），
   而不是报 `DEEPSEEK_API_KEY` 缺失。

### 9.1 访问 `/deepseek-harness/` 报 500 时怎么定位

先分清 500 是哪一层抛的，看页面右下角的版本号：

- `nginx/1.22.1` → **DSH 容器内层** Nginx（Debian bookworm）抛的，说明外层 4000
  分流正常，问题在容器内部；
- `nginx/1.27.x`（alpine）→ 外层 `gateway-edge` 抛的，通常是 `deepseek-harness`
  容器没起来或不在 compose 网络上。

内层 500 有三个来源：`auth_request` 调 provisioner 失败、provisioner 没有返回
`X-DSH-Port` 导致 `proxy_pass http://127.0.0.1:$dsh_port` 里的变量为空，或者
**Nginx worker 读不到 `/etc/dsh-auth/htpasswd`**（basic auth 校验失败）。

关于 provisioner：**`auth_request` 会把子请求的响应体丢掉，只对外报一个裸
500**。所以从浏览器上看不到 provisioner 的真实报错，必须看容器日志：

```bash
docker compose logs --tail=200 deepseek-harness
```

`provision.py` 在探测用户实例失败时会打印：

```text
provisioner: provision failed for user='alice': dsh web for alice exited with code 1; ...
```

并附上该用户 `dsh.log` 的末尾几十行。也可以直接看：

```bash
docker compose exec deepseek-harness \
  sh -c 'tail -n 100 /data/users/<username>/dsh.log'
```

已针对三个常见根因做了处理：

1. **首次请求的启动竞态**：provisioner 现在会等新拉起的 `dsh web` 真正监听端口
   （默认最多 120 秒，可用 `DSH_STARTUP_TIMEOUT_SECONDS` 调整）才返回 204；否则
   Nginx 会把「端口还没起来」变成裸 500。
2. **provisioner 自身还没监听 3090**：`start.sh` 现在会先等 `127.0.0.1:3090`
   可连接，再启动 Nginx，避免启动瞬间的请求全部 auth 失败。
3. **`/etc/dsh-auth/htpasswd` 权限不足**：Nginx worker 以非 root 用户（Debian 下是
   `www-data`）运行，而 `auth_basic_user_file` 是 worker 在处理「带凭据的请求」
   时才打开的。历史上 `start.sh` 把 htpasswd 设成 `0600 root:root`，于是：
   匿名请求仍然返回 401（Nginx 不需要读文件就能拒绝），一旦浏览器带上用户名
   密码就变成 `[crit] open() "/etc/dsh-auth/htpasswd" failed (13: Permission denied)`
   加 500 —— 症状就是「不输密码提示 401，输对了反而 500」。
   现在 `start.sh` 每次启动都执行 `fix_htpasswd_perms`：优先
   `chown root:www-data` + `chmod 0640`（只有 root 和 Nginx worker 组可读），
   环境里没有 `www-data` 时退化成 `0644`。因为 htpasswd 存在 named volume 里，
   旧版本写出的 `0600` 会一直残留，所以这一步是「每次启动都修」而不是
   「只在首次创建时修」。

   排查命令：

   ```bash
   docker compose exec deepseek-harness sh -c 'ls -ld /etc/dsh-auth; ls -l /etc/dsh-auth/htpasswd'
   # 期望 drwxr-x--- 1 root www-data /etc/dsh-auth
   # 期望 -rw-r----- 1 root www-data
   ```

另外配置里的 `DSH_STARTUP_TIMEOUT_SECONDS` 也可以直接加进根目录 `.env`。

如果页面是 `403 Forbidden`，先看 Harness 日志里是否有：

```text
open() "/etc/dsh-auth/htpasswd" failed (2: No such file or directory)
```

这不是密码错误，而是运行中的镜像仍是旧版本，或 `dsh_auth` 卷为空且旧版
`start.sh` 没有创建新路径的认证文件。当前版本的 `start.sh` 会在每次启动时按
`DSH_BOOTSTRAP_USER` / `DSH_BOOTSTRAP_PASSWORD` 创建或修复
`/etc/dsh-auth/htpasswd`；确认已同步最新外层文件后执行：

```bash
docker compose up -d --build --force-recreate deepseek-harness gateway-edge
docker compose exec deepseek-harness sh -c \
  'ls -ld /etc/dsh-auth; ls -l /etc/dsh-auth/htpasswd'
```

如果日志同时出现：

```text
settings-file: invalid document at /data/users/<user>/.dsh/settings.yaml
```

说明命名卷里还留着早期错误模板。当前 provisioner 会识别
`DSH_DEPLOY_SETTINGS_V4` 版本标记并自动重写；若容器尚未用新镜像启动，可先删除
这一个已经损坏的文件再重启（不会删除会话目录和 workspace）：

```bash
docker compose exec deepseek-harness sh -c \
  'rm -f /data/users/<user>/.dsh/settings.yaml'
docker compose restart deepseek-harness
```

如果 settings 已经是当前版本，但日志出现：

```text
EACCES: permission denied, open '/data/users/<user>/.dsh/profiles/web/cordis.yml'
```

通常是旧版 root 进程留下了 root-owned 子文件，而用户根目录本身的 owner 已经
被改成了新 UID，导致旧版的“只检查顶层 owner”逻辑跳过递归修复。当前 provisioner
会在每个用户首次访问时递归接管一次该用户树；升级前也可以临时按 UID 映射手工修复：

```bash
docker compose exec deepseek-harness sh -c \
  'cat /etc/dsh-auth/.uidmap'
# 假设输出 mengweiming:20000
docker compose exec deepseek-harness sh -c \
  'chown -R --no-dereference 20000:20000 /data/users/mengweiming /workspaces/mengweiming'
docker compose restart deepseek-harness
```

Docker 里的 Harness 没有桌面环境，不能让容器直接调用宿主机编辑器。当前部署保留
“打开配置文件”按钮，但由网关注入的浏览器脚本把它改为打开：

```text
/deepseek-harness/config/
```

这是当前 Basic Auth 用户自己的浏览器内 YAML 编辑器。需要直接查看当前用户实际
配置时，也可以在服务器执行：

```bash
docker compose exec deepseek-harness sh -c \
  'sed -n "/llm-pi-ai:/,$p" /data/users/<user>/.dsh/settings.yaml'
```

也可以直接访问：

```text
http://<host>:4000/deepseek-harness/config/
```

编辑器只允许当前 Basic Auth 用户读取和写入自己的 `settings.yaml`。保存前使用
DSH 自带 YAML 解析器校验，并通过 ETag 防止覆盖其他标签页刚保存的版本；保存前
保留 `settings.yaml.bak`。实际模型 API key 位于独立的 `.credentials.yaml`，
不会由这个页面读取或返回。

编辑器保存前会做 YAML 语法校验、ETag 并发检查，并在原文件旁保留
`settings.yaml.bak`；API key 不在 settings.yaml 中，不会被页面展示。

如果选择器里显示的是自定义 provider（例如 `model-gateway`），要重点确认该
provider 的每个模型条目下是否有 `reasoningEfforts`；部署模板对
`private-gateway` 生成的档位不会自动附加到另一个自定义 provider。

当前部署插件还会监听 `llm-pi-ai` 的网页配置更新：对自定义 provider 中没有显式
关闭 reasoning 的模型，自动补齐 `off`/`low`/`high`/`max` 四档和
`compat.supportsReasoningEffort`。因此重新配置 `model-test` 后无需手工编辑 YAML；
更新后刷新页面或新建会话即可看到推理等级。

### 9.2 认证后仍然 404 / 401：DSH 的浏览器会话 cookie

现象：Basic Auth 已经通过（`docker compose exec deepseek-harness sh -c 'ls -l
/etc/dsh-auth/htpasswd'` 正常），但 `curl -u 用户:密码 http://127.0.0.1:3080/` 返回
**404** 或 **401**，而不是页面。

根因在 DSH 自身：`packages/client/connection/src/browser-auth.ts` 的
`authorizeIndex()` 只在这两种情况下才把 `index.html` 发出来：

1. 请求恰好是 `GET /?token=<launchToken>`（只允许一个 token 参数、路径恰好是
   `/`）→ 回 `303` + `Set-Cookie`，把浏览器送回干净的 `/`；
2. 请求带一个由本进程 secret 签名、且 **`authority` 与本次请求 Host 匹配**的
   `dsh-auth-<sha256(authority)>` cookie。

其它情况一律 `401`。而 `token` 只在 `dsh web` 打印一次：

```text
dsh web: http://127.0.0.1:31003/?token=<base64url> (LAN: ...)
```

它出现在 Loader 树结束之后，所以「端口能连」不等于「能取到 token」——
早于这一行去请求还会先撞进「fallback 尚未挂载」的空档，得到 `404`。

处理办法（本次实现）：

- `provision.py` 先以 `HTTP GET /` 探测，只有 `200/303/401` 才算“已挂载”
  （`404` 说明 frontend-static 还没接管 fallback）；随后从该用户 `dsh.log`
  新增的字节里用正则抓出本进程的 token（只扫启动后追加的部分，避免拿到上一次
  运行的旧 token），超时由 `DSH_TOKEN_WAIT_SECONDS`（默认 10 秒）控制；
- 抓到 token 后，provisioner 自己发一次 `GET /?token=...`（同样带
  `Host: 127.0.0.1`）完成兑换，把响应里的 `name=value` 作为 `X-DSH-Cookie`
  返回给内层 Nginx；
- 内层 `nginx.conf` 用 `auth_request_set` 取出它，并通过
  `proxy_set_header Cookie $dsh_cookie` 覆盖浏览器带来的 Cookie。

这样浏览器侧完全不需要处理 `/ ?token=` 跳转，也就没有“每次都重定向”的
死循环风险；用户在浏览器里只需要过一次 Basic Auth。`Cookie` 覆盖还顺带解决了
多用户共用同一个 `Host` 的问题：cookie 名只取决于 authority，只有由服务端
按用户注入，才不会互相串号。

如果 `dsh.log` 里找不到 token，provisioner 会报：

```text
provisioner: provision failed for user='alice': no DSH launch token found in ...
```

此时 `docker compose restart deepseek-harness` 让 DSH 重新打印一次即可。

**会话 cookie 的续期**：DSH 的签名 cookie 是 authority 绑定 + 硬过期（默认
30 天），而过期后它**不会再签发**——能签发新 cookie 的只有一次性的
`?token=` 兑换，token 又是进程启动时随机生成、只打印一次的。因为持有 cookie
的是 provisioner 而不是浏览器，容器一旦连续运行到第 30 天，所有请求都会
开始 401（`dsh web` 进程本身还活着，token 也没变）。

为此 `provision.py` 会记录每个用户上次兑换 cookie 的时间：
`_ensure_cookie()` 只有在缓存 cookie 的“年龄”小于 `COOKIE_REFRESH_SECONDS`
（默认 `12 * 3600`，即 12 小时）时才直接复用，否则用同一颗 token 重新兑换
一次（兑换接口对同一颗 token 可重复调用，每次都返回新的 `Set-Cookie`）。
这样正常请求永远拿到的是刚兑换不久、离过期还很远的 cookie，30 天硬过期
在服务端被悄悄绕开，用户无感。要调整刷新间隔，在 `.env` 里设
`DSH_COOKIE_REFRESH_SECONDS` 即可（改成远大于 12 小时的值会让 cookie 更接近
过期点，不建议）。

### 9.3 页面能开、但报“模型/设置不可用”或 `MISSING_CREDENTIAL`

这两个报错是一对孪生问题，且 B 不修好时 A 没法从 UI 里自救：

| 现象 | 根因 | 修复位置 |
| --- | --- | --- |
| `加载提供方目录失败: settings are unavailable in this browser` | 非回环 hostname 触发 DSH 的 `memory` 持久化分支 | `gateway-nginx.conf` 的 `__DSH_TRANSPORT__` 注入（§5.1） |
| `no API key for provider route "deepseek-official" (MISSING_CREDENTIAL)` | 内置默认模型仍指向 `deepseek-official` | `settings.yaml` 的 `agent-default-model` 段（§4.1） |

排查顺序：

1. 刷新页面确认设置页能打开，说明 `__DSH_TRANSPORT__` 生效；
2. 打开 **设置 → 模型 → Private LiteLLM Gateway**，填入 LiteLLM virtual key；
3. 新建会话发一条消息，确认不再出现 `deepseek-official` 报错。

如果设置页仍然不可用，检查用户 `settings.yaml` 是不是旧版本：

```bash
docker compose exec deepseek-harness \
  sh -c 'grep -c agent-default-model /data/users/<username>/.dsh/settings.yaml'
```

返回 `0` 说明是旧文件。当前 `provision.py` 会在下次请求时自动重新渲染；
也可以直接删掉让它重建：

```bash
docker compose exec deepseek-harness \
  sh -c 'rm -f /data/users/<username>/.dsh/settings.yaml'
```

### 9.4 `/v1/responses` 返回 `413 Request Entity Too Large`

现象：Codex / Claude Code 发一条较大的请求（长上下文、大附件、整段历史）时，
客户端直接弹出：

```text
unexpected status 413 Payload Too Large: <html>...<center>nginx/1.27.5</center>...
url: http://<host>:4000/v1/responses
```

根因**不是**模型上下文长度，而是 Nginx 的**传输层**限制：`client_max_body_size`
默认只有 `1m`，超过就在**请求还没到 LiteLLM** 时被拒。`nginx/1.27.x` 这个版本号
是外层 `gateway-edge`（`nginx:1.27-alpine`）打印的，说明拦截发生在外层网关
（内层 DSH Nginx 是 Debian bookworm 的 `1.22.1`，详见 §9.1 的版本号对照表）。

修法：两层 Nginx 的 `http {}` 里都设 `client_max_body_size 512m;`（见 §5 的
“容易出错的点”）。改完重建/重启对应容器：

```bash
docker compose up -d --force-recreate gateway-edge deepseek-harness
```

验证（应返回一次正常的 LiteLLM 响应，或至少不是 413）：

```bash
# 造一个 >1m 的 body，确认不再 413
python3 -c "print('{\"model\":\"<model>\",\"messages\":[{\"role\":\"user\",\"content\":\"' + 'x'*2000000 + '\"}]}')" > /tmp/big.json
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer <key>' \
  --data-binary @/tmp/big.json http://127.0.0.1:4000/v1/chat/completions
```

如果仍报 413 但版本号是 `1.22.1`，说明是 DSH 内层 Nginx（例如 Agent 在网页里
上传大附件）拒的，同样由上面 `client_max_body_size` 覆盖。

### 9.5 Agent 每次跑命令都要审批（沙箱后端不可用）

现象：Agent 在网页里执行任何命令（哪怕只是 `ls`）都会弹出「是否允许」的审批，
或者在日志里看到 sandbox 相关的 `SandboxUnavailableError`：

```text
sandbox-local: no usable runner for linux-x64 (bwrap, landlock)
```

根因**不在权限配置本身**，而在宿主内核 / 容器隔离能力：DSH 的 Linux 沙箱是一条
**候选链**，按顺序探测，第一个可用的胜出（见上游
`packages/sandbox/sandbox-local/src/index.ts` 的 `PLATFORM_CHAINS.linux`）：

```text
linux: ['bwrap', 'landlock']
```

两个候选都是**功能性探测**（不是看一眼有没有这个命令）：

- **bwrap**：跑 `bwrap --ro-bind / / --dev /dev --unshare-pid --proc /proc
  --die-with-parent -- true`。它需要 `unshare(CLONE_NEWUSER)`，而 **Docker 的
  默认 seccomp profile 会拦截 `CLONE_NEWUSER`**，于是探测失败；
- **landlock**：跑 `<pkg>/bin/landlock-run --probe`。`landlock-run` 是静态 musl
  小程序，需要**内核 ≥5.13 且 `CONFIG_SECURITY_LANDLOCK=y`、LSM 已启用**。

本部署的目标宿主是 **AlmaLinux 8.10，内核 `4.18.0-553.58.1.el8_10.x86_64`**。
RHEL 系的 4.18 内核**不含 Landlock**（它 5.13 才进主线），所以：

- landlock probe 必然返回 `unusable`（无论镜像里有没有那个二进制、编没编出来）；
- bwrap 探测要么因为**没装**、要么因为**seccomp 拦 user namespace** 失败。

两个都不可用 → Linux 链没有可用 runner → DSH 拒绝沙箱化命令。它既不肯静默地
「无沙箱执行」，又不能在用户确认前执行，于是就表现为**每次调用都要审批**，且
审批过了也只是走 `danger-full-access` 重试一次。

**为什么「在 Dockerfile 里补编 landlock-run」救不了这台机器**：即使把
`landlock-run` 编出来放进镜像，`--probe` 在 4.18 内核上照样返回 `unusable`。
这是**宿主机能力**问题，不是镜像缺文件的问题。

#### 方案 A（推荐，本机立即可用）：关掉审批

`.env` 里设：

```dotenv
DSH_PERMISSION_MODE=danger-full-access
```

DSH 的 `packages/bundle/base/cordis.patch.yml` 用这个变量同时决定两件事：

| 变量 | 沙箱模式 | 审批策略 |
| --- | --- | --- |
| 未设置（默认） | `workspace-write` | `ask`（每次都问） |
| `danger-full-access` | `danger-full-access` | `never`（不问） |

效果：**审批弹窗消失**。代价：**文件系统沙箱关闭** —— Agent 的 bash 不再限定在
会话 workspace 内，可以读写该用户 UID 能触达的任意路径。

对多用户部署而言这个代价是可接受的，因为**访问边界仍然成立**：每个用户的
`dsh web` 仍以**自己的专属 UID** 运行（见 §3.3），因此

- 读不到 `/etc/dsh-auth/htpasswd`（不在 `www-data` 组、且目录 0750）；
- 读不到也写不到 `/data/users/<other>`、`/workspaces/<other>`（0700 且属别人）；
- 能触达的「任意路径」只到它自己 UID 的权限边界为止。

换句话说：丢掉的是「单用户内部的纵深防御」（防的是**用户自己**命令乱跑），
保住的是「用户之间的隔离」（防的是**别人**偷看）——后者才是多租户部署真正要守的。

#### 方案 B（需宿主机配合）：把沙箱后端跑通

要恢复 `workspace-write` 文件沙箱，必须让 bwrap 或 landlock 之一真正可用：

1. **bubblewrap + 放开 seccomp**（改动最小）：
   - `bubblewrap` 已在镜像的 apt 列表里（Dockerfile），无需额外装；
   - 给 `deepseek-harness` 容器一个**放开 user namespace 的 seccomp profile**
     （或临时用 `security_opt: [seccomp=unconfined]`）；
   - 另需确认宿主机 `user.max_user_namespaces` 不是 0（RHEL 8 加固基线常把它
     设为 0）。
   - ⚠️ 这会削弱容器隔离，和 §7「禁止 `--privileged`」的精神部分冲突。请评估
     后再上，且**不要**顺手加 `--privileged`。
2. **换现代内核**：宿主机内核升到 **≥5.13** 且启用 Landlock，重启后 landlock
   probe 通过，`workspace-write` 自动可用，无需改镜像。

方案 B 属于**基础设施变更**，要单独评审；在本机（4.18 内核）上不生效。

#### 验收

```bash
# 1) 确认开关已传到容器
docker compose exec deepseek-harness sh -c 'echo $DSH_PERMISSION_MODE'

# 2) 重建并重启
docker compose up -d --build --force-recreate deepseek-harness gateway-edge

# 3) 在网页里让 Agent 跑一条命令（如 `ls /workspaces/<user>`）
#    预期：不再弹审批，直接返回结果
```

若设了 `danger-full-access` 之后仍在弹审批，检查：

- 变量名是否是 `DSH_PERMISSION_MODE`（不是 `DSH_PERMISSION`）；
- 容器是否真的重建了（用上面第 1) 条确认）；
- 是否在**旧的 named volume** 里残留了会覆盖 sandbox 的用户级补丁
  （必要时按 §9.3 清一次用户设置再重跑）。

## 10. 当前限制

- Basic Auth 不是完整账号系统，没有找回密码、组织、角色和审计页面；
- 用户必须由管理员预先加入 `htpasswd`，首次请求才创建 workspace；
- 每个用户的 LiteLLM virtual key 由用户自己在网页里填写，容器不再下发共享 key；
- 联网搜索依赖同 compose 的 `searxng-mcp` 服务，两者必须一起启动；内置
  `deepseek-official` 搜索已被 home 补丁关闭（见 §4.4）；
- 用户隔离只到「容器内每用户一个 UID」这一层（见 §3.3）：文件互不可见，但仍共享
  同一个容器内核、网络和内存/PID 配额，不是不可信公网用户之间的强隔离；
- 用户删除目前需要手工做两件事：从 htpasswd 删账号、删 `/data/users/<user>` 和
  `/workspaces/<user>`；UID 映射里那一行可以留着（无害），也可以一并删掉。
- 一个用户对应一个长期运行的 DSH 进程，用户多时要提高内存和 PID 限额；
- 命令沙箱在 AlmaLinux/CentOS/RHEL 8（内核 4.18）上不可用，默认必须靠
  `DSH_PERMISSION_MODE=danger-full-access` 关掉逐次审批；这会关闭单用户文件
  沙箱（用户间 UID 隔离不受影响）。要恢复文件沙箱需升级宿主内核或放开容器
  user namespace，见 §9.5；
- 当前版本的 DSH/插件协议仍可能变化，升级版本必须重新执行完整验证；
- 上游源码目录会被 `COPY` 进镜像，但本身不进根仓库；升级版本要同步改
  `DSH_SRC_DIR` 和 `DSH_COMMIT_HASH`，并重新执行完整验证。
