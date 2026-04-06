FROM python:3.12-slim

WORKDIR /app

# Install gh CLI for GitHub adapter
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Non-root user for K8s runAsNonRoot enforcement
RUN groupadd -r brain && useradd -r -g brain -d /app brain \
    && mkdir -p /app/data /app/config \
    && chown -R brain:brain /app

COPY --chown=brain:brain requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true

COPY --chown=brain:brain src/ ./src/
COPY --chown=brain:brain config/brain.yaml config/projects.json ./config/

ENV PYTHONPATH=/app/src

USER brain

EXPOSE 8790

ENTRYPOINT ["python", "-m", "unified_brain"]
CMD ["--config", "config/brain.yaml", "--health-port", "8790"]
