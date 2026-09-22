FROM node:22.23.2-bookworm-slim

ARG DSH_REPO=https://github.com/deepseek-ai/deepseek-harness.git
ARG DSH_TAG=dsh-v0.1.5-rc.2

ENV DEBIAN_FRONTEND=noninteractive \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    NODE_ENV=production

RUN apt-get update && apt-get install -y --no-install-recommends \
        apache2-utils \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        nginx \
        pkg-config \
        python3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable && corepack prepare pnpm@11.7.0 --activate

RUN git clone --depth 1 --branch "${DSH_TAG}" "${DSH_REPO}" /opt/dsh

RUN cd /opt/dsh \
    && pnpm install --frozen-lockfile \
    && pnpm run build

WORKDIR /opt/dsh

COPY nginx.conf /etc/nginx/nginx.conf
COPY provision.py /usr/local/bin/dsh-provision.py
COPY settings.yaml /etc/dsh/settings.yaml
COPY start.sh /usr/local/bin/start-dsh

RUN chmod 0755 /usr/local/bin/start-dsh /usr/local/bin/dsh-provision.py \
    && mkdir -p /data /workspaces /run/nginx \
    && rm -f /etc/nginx/sites-enabled/default

EXPOSE 3080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-dsh"]
