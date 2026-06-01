FROM node:24-slim

ARG CLAUDE_CODE_VERSION=2.1.156
ARG OPENAI_CODEX_VERSION=0.135.0

# Stage 0 only runs isolated echo/id commands under /wd/v1/run. The CLI pins are
# kept here for the Stage 2 claude/codex handoff; update deliberately, not via latest.
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} @openai/codex@${OPENAI_CODEX_VERSION} && \
    apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip ca-certificates tini && \
    python3 -m pip install --break-system-packages --no-cache-dir fastapi uvicorn[standard] && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.claude/users /root/.codex/users && \
    ln -sf /root/.claude/.claude.json /root/.claude.json

WORKDIR /app
COPY server.py login_core.py wd_security.py wd_server.py supervisor.py ./
RUN mkdir -p /app/workdir /data/auth/claude/users /data/auth/codex/users /data/ws /data/tmp

EXPOSE 9100 9101
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "supervisor.py"]
