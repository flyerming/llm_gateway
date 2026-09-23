FROM node:22.23.2-bookworm-slim

# 源码来源：直接使用构建上下文里的本地上游 checkout，相对路径，不再在构建阶段
# 执行 git clone，从而彻底摆脱「镜像里拉 GitHub 失败/超时」的问题。
# 目录名与官方 tag 一一对应；迁移到新环境时，把官方原始下载的源码放在同一相对
# 路径下即可（构建上下文是 deepseek-harness/ 目录）。
ARG DSH_SRC_DIR=deepseek-harness-dsh-v0.1.5-rc.2-git

# 该官方 tag 对应的 commit。.git 目录不进入镜像，构建元数据（页面右下角的版本/
# commit）就靠这个变量补齐，否则 DSH 构建脚本会尝试调用 git rev-parse 而拿不到值。
ARG DSH_COMMIT_HASH=fb2c4b9e698e30edb738bca4cf0618587db7d203

# npm/pnpm 依赖下载使用的 registry。
# 默认走国内镜像，避免国内服务器直连 registry.npmjs.org 时出现
# error (23)/超时，导致 pnpm install 反复重试后构建失败。
# 需要时可在 .env 里改成 https://registry.npmjs.org 或私有 Nexus/Verdaccio。
ARG NPM_REGISTRY=https://registry.npmmirror.com

# pnpm 同时在跑的下载连接数。
# 默认 16，在国内网络/受限出口下很容易被中间设备 RST，表现为
# `[WARN] GET ... error (23). Will retry in 1 minute.`（响应写入本地失败，
# 或中间代理在大文件传输时中断）。
# 调低到 4 牺牲一点速度换稳定性，需要时可在 .env 覆盖。
ARG NPM_FETCH_CONCURRENCY=4

# 构建期代理（可选）。
# 宿主机靠 mihomo / 公司代理出网时（典型症状：宿主机能跑通 pnpm，到 docker
# build 里连 npm 源就被重置），把同一个代理地址通过 .env 传进来。
#
# 关键：docker 的 build 不会继承宿主机的 *_proxy，必须显式声明 ARG。
# ARG 在构建阶段本身就作为环境变量对 RUN 可见，所以 pnpm / corepack / git /
# curl 都会自动走它；同时 ARG 不会烧进最终镜像，运行期容器不受影响。
#
# compose 里构建阶段用的是 `network: host`，所以构建容器和宿主机共用网络栈，
# 这里的 127.0.0.1 就是宿主机本身，可直接指向宿主机上的 mihomo（7890）。
# （若哪天去掉 network: host，则要改成宿主机在 docker0 上的地址，如 172.17.0.1。）
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG NO_PROXY=

# Debian APT 镜像源（留空时使用 node 基础镜像自带的 deb.debian.org）。
# 国内服务器无法直连 Debian 时，可在 .env 里覆盖为可达的镜像站。
ARG APT_MIRROR=
ARG APT_SECURITY_MIRROR=

# COREPACK_NPM_REGISTRY 让 corepack 下载 pnpm 自身也走镜像，
# 否则 corepack prepare 这一步同样会卡在 registry.npmjs.org。
# 注意：这里不设 NODE_ENV=production —— pnpm 在 production 下会跳过
# devDependencies，而构建工具链（tsx/typescript 等）就在 devDependencies 里。
# 运行期需要的 NODE_ENV 在构建完成后单独设置。
ENV DEBIAN_FRONTEND=noninteractive \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    COREPACK_NPM_REGISTRY=$NPM_REGISTRY

# bubblewrap 是 DSH 沙箱链的第一个候选（bwrap → landlock）。装上它不影响
# 4.18 内核宿主上的现状（Docker 默认 seccomp 仍会拦 CLONE_NEWUSER，probe
# 照样失败），但它是「放开 seccomp 后即可恢复文件沙箱」这条路的唯一前置条件，
# 装了不亏。详见 deepseek-harness/README.md §9.5。
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ] && [ -n "${APT_SECURITY_MIRROR}" ]; then \
        rm -f /etc/apt/sources.list.d/debian.sources; \
        printf '%s\n' \
            "deb ${APT_MIRROR} bookworm main" \
            "deb ${APT_MIRROR} bookworm-updates main" \
            "deb ${APT_SECURITY_MIRROR} bookworm-security main" \
            > /etc/apt/sources.list; \
    fi; \
    export http_proxy="${HTTP_PROXY}" \
           https_proxy="${HTTPS_PROXY}" \
           no_proxy="${NO_PROXY}" \
           HTTP_PROXY="${HTTP_PROXY}" \
           HTTPS_PROXY="${HTTPS_PROXY}" \
           NO_PROXY="${NO_PROXY}"; \
    apt-get -o Acquire::Retries=5 \
            -o APT::Update::Error-Mode=any \
            update; \
    apt-get install -y --no-install-recommends \
        apache2-utils \
        bash \
        bubblewrap \
        build-essential \
        ca-certificates \
        curl \
        git \
        nginx \
        pkg-config \
        python3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

# 写入 npm/pnpm 网络配置：
#   registry            依赖下载源；
#   network-concurrency 降低并发，避免被中间设备批量重置；
#   child-concurrency   同时执行的构建子进程数；
#   fetch-*             默认只重试 2 次且超时偏短，这里放宽次数与超时。
RUN printf '%s\n' \
        "registry=$NPM_REGISTRY" \
        "network-concurrency=$NPM_FETCH_CONCURRENCY" \
        "child-concurrency=2" \
        "fetch-retries=5" \
        "fetch-retry-mintimeout=10000" \
        "fetch-retry-maxtimeout=120000" \
        "fetch-timeout=600000" \
        > /root/.npmrc \
    && corepack enable \
    && corepack prepare pnpm@11.7.0 --activate

# 直接把本地上游源码复制进镜像，构建阶段完全不访问 GitHub。
# 相对路径 DSH_SRC_DIR 是相对构建上下文（compose 里 context: ./deepseek-harness）。
# 官方 checkout 的目录名里带版本号，升级版本时改 compose/Dockerfile 里的
# DSH_SRC_DIR、DSH_COMMIT_HASH 两个参数即可。
COPY ${DSH_SRC_DIR}/ /opt/dsh/

# .git 不进入构建上下文（见 .dockerignore），所以这里显式校验源码确实存在，
# 避免 COPY 空目录后到 pnpm install 阶段才报错。
RUN test -f /opt/dsh/package.json \
    && test -f /opt/dsh/pnpm-lock.yaml

# install 分成两步并各自重试：
#   1) pnpm fetch：只下载 store，已下好的包会保留；
#   2) pnpm install --offline：从本地 store 链接，不再访问网络。
# 这样即使镜像源中途抽风，重跑也只补缺失的那部分，不会从头再来。
RUN cd /opt/dsh \
    && for i in 1 2 3 4 5; do \
        echo "== pnpm fetch (attempt $i) =="; \
        pnpm fetch --frozen-lockfile && break || sleep 10; \
    done \
    && pnpm install --frozen-lockfile --offline

# DSH_CLIENT_COMMIT_HASH 用于补齐构建元数据：源码目录没有 .git，
# 构建脚本无法调用 git rev-parse，用官方 tag 对应 commit 代替。
RUN cd /opt/dsh \
    && DSH_CLIENT_COMMIT_HASH=${DSH_COMMIT_HASH} pnpm run build

WORKDIR /opt/dsh

# 构建完成后才标记为生产模式，避免影响上面的 install/build。
ENV NODE_ENV=production

COPY nginx.conf /etc/nginx/nginx.conf
COPY provision.py /usr/local/bin/dsh-provision.py
COPY settings.yaml /etc/dsh/settings.yaml
# DSH 的 home 级补丁模板是可选的：provision.py 内置同一份兜底内容，避免
# 迁移环境时漏掉尚未加入 Git 的新文件导致 Dockerfile COPY 直接失败。
# 若构建上下文里存在 cordis.patch.yml，provision.py 会优先使用它。
COPY start.sh /usr/local/bin/start-dsh

RUN chmod 0755 /usr/local/bin/start-dsh /usr/local/bin/dsh-provision.py \
    && mkdir -p /data /workspaces /run/nginx /etc/dsh-auth \
    && rm -f /etc/nginx/sites-enabled/default

EXPOSE 3080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-dsh"]
