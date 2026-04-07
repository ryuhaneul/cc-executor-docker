FROM node:20-slim

RUN npm install -g @anthropic-ai/claude-code && \
    apt-get update && apt-get install -y --no-install-recommends python3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py .
RUN mkdir -p /app/workdir

EXPOSE 9100
CMD ["python3", "server.py"]
