FROM node:20-slim

RUN npm install -g @anthropic-ai/claude-code && \
    apt-get update && \
    apt-get install -y --no-install-recommends python3 ca-certificates && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.claude && \
    ln -sf /root/.claude/.claude.json /root/.claude.json

WORKDIR /app
COPY server.py .
RUN mkdir -p /app/workdir

EXPOSE 9100
CMD ["python3", "server.py"]
