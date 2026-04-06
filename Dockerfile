FROM python:3.12-slim

WORKDIR /app

# Non-root user for K8s runAsNonRoot enforcement
RUN groupadd -r brain && useradd -r -g brain -d /app brain \
    && mkdir -p /app/data /app/config \
    && chown -R brain:brain /app

COPY --chown=brain:brain requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=brain:brain src/ ./src/
COPY --chown=brain:brain config/brain.yaml config/projects.json ./config/

USER brain

EXPOSE 8790

ENTRYPOINT ["python", "-m", "unified_brain"]
CMD ["--config", "config/brain.yaml", "--health-port", "8790"]
