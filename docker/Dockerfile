FROM node:22-bookworm

RUN apt-get update \
&& apt-get install -y --no-install-recommends \
curl \
git \
iproute2 \
jq \
tar \
bubblewrap \
python3 \
python-is-python3 \
&& rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex ws

COPY codeqc-ws-monitor /opt/codeqc-ws-monitor
RUN npm install -g /opt/codeqc-ws-monitor

WORKDIR /workspace

RUN mkdir -p /root/.codex && \
    printf 'check_for_update_on_startup = false\n' > /root/.codex/config.toml && \
    printf 'approval_policy = "never"\n' >> /root/.codex/config.toml && \
    printf 'sandbox_mode = "danger-full-access"\n\n' >> /root/.codex/config.toml && \
    printf '[projects."/workspace"]\n' >> /root/.codex/config.toml && \
    printf 'trust_level = "trusted"\n' >> /root/.codex/config.toml

CMD ["bash"]
