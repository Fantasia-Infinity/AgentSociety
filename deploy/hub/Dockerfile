FROM python:3.13-slim

RUN useradd --create-home --uid 10001 agenthub \
    && mkdir -p /data \
    && chown agenthub:agenthub /data
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[postgres,s3,oidc]'

USER 10001:10001
ENV AGENT_HUB_HOST=0.0.0.0 \
    AGENT_HUB_PORT=8090 \
    AGENT_HUB_STATE_DB=/data/hub-state.sqlite3 \
    AGENT_HUB_ALLOW_NON_LOOPBACK_BIND=true
EXPOSE 8090
VOLUME ["/data"]
CMD ["agent-hub"]
