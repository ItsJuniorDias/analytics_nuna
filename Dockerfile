# Nuna Analytics: FastAPI + SQLite num único processo.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NUNA_DB_PATH=/data/analytics.db \
    PORT=8000

WORKDIR /srv

# Usuário sem privilégios. /data é o ponto de montagem do volume
# (docker-compose) ou do disco persistente (Render). Não há instrução VOLUME
# de propósito: o Render não aceita, e o compose declara o volume nomeado.
RUN groupadd --system --gid 10001 nuna \
    && useradd --system --uid 10001 --gid nuna --home-dir /srv --shell /usr/sbin/nologin nuna \
    && mkdir -p /data \
    && chown nuna:nuna /data

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY catalog ./catalog

USER nuna

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# --no-access-log: o access log do uvicorn grava o IP de cada requisição.
# --no-proxy-headers: não copia X-Forwarded-For para o endereço do cliente.
# --workers 1: SQLite com um escritor e a limpeza agendada rodando uma vez só.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT}\" --workers 1 --no-access-log --no-proxy-headers --no-server-header"]
