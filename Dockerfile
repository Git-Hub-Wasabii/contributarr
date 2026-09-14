FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG APP_VERSION=v26.09.14
LABEL org.opencontainers.image.title="Contributarr" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="Permanent storage contribution tracking for Seerr media requests" \
      org.opencontainers.image.source="https://github.com/Git-Hub-Wasabii/Contributarr"
WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /data/sessions && chmod 0755 /app/docker-entrypoint.sh && chown -R 10001:10001 /app /data
ENTRYPOINT ["/app/docker-entrypoint.sh"]
EXPOSE 9096

CMD ["sh", "-c", "flask --app app:create_app db upgrade && exec gunicorn --workers 1 --threads 4 --bind 0.0.0.0:9096 'app:create_app()'"]
