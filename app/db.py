"""SQLite: conexão, pragmas e migrações.

sqlite3 da biblioteca padrão, uma conexão por operação. Com um único processo
uvicorn e WAL, isso aguenta com folga o volume de um app infantil e evita
compartilhar conexão entre threads do threadpool do FastAPI.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, List, Sequence, Tuple

BUSY_TIMEOUT_MS = 5000

# Cada migração é aplicada uma vez, em ordem, e registrada em schema_migrations.
# Nunca editar uma migração já publicada: criar a próxima.
MIGRATIONS: Sequence[Tuple[int, Sequence[str]]] = (
    (
        1,
        (
            # WITHOUT ROWID com event_id (UUID aleatório) como chave: as linhas
            # ficam ordenadas por um valor aleatório, não pela ordem de chegada.
            # Numa tabela comum, o rowid sequencial deixaria contíguos os
            # eventos de um mesmo upload, e um upload pode trazer várias
            # sessões do mesmo aparelho; isso permitiria juntar sessões, que a
            # política promete não fazer.
            #
            # Não existe coluna de IP, User-Agent ou identificador de aparelho.
            # tests/test_privacy.py falha se alguém criar uma.
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY NOT NULL,
                name TEXT NOT NULL,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                received_at TEXT NOT NULL,
                catalog_version INTEGER NOT NULL,
                app_version TEXT NOT NULL,
                build TEXT NOT NULL,
                os_version TEXT NOT NULL,
                device_family TEXT NOT NULL,
                layout TEXT,
                subscription_state TEXT NOT NULL,
                properties TEXT NOT NULL
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_events_name_ts ON events (name, ts)",
            "CREATE INDEX IF NOT EXISTS idx_events_session_id ON events (session_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_received_at ON events (received_at)",
            "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
            # O que sobra depois da retenção: só totais por dia e nome, sem
            # session_id, como promete a política de privacidade.
            """
            CREATE TABLE IF NOT EXISTS daily_event_counts (
                day TEXT NOT NULL,
                name TEXT NOT NULL,
                events INTEGER NOT NULL,
                PRIMARY KEY (day, name)
            ) WITHOUT ROWID
            """,
        ),
    ),
    (
        2,
        (
            # País da conta da App Store (ISO alfa-3), não localização do
            # aparelho. NULL em eventos antigos e quando o StoreKit não informa.
            "ALTER TABLE events ADD COLUMN storefront TEXT",
        ),
    ),
)

_initialized = set()
_init_lock = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _open(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # isolation_level=None: transações explícitas (BEGIN IMMEDIATE) em vez do
    # BEGIN implícito do módulo, que abre transação de leitura e depois falha
    # ao tentar escrever com outro escritor ativo.
    # check_same_thread=False: o FastAPI pode abrir e fechar a conexão em
    # threads diferentes do threadpool; nunca há uso simultâneo.
    conn = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA foreign_keys = ON")
    # NORMAL em WAL: um crash do SO pode perder a última transação, nunca
    # corromper o banco. Aceitável para analytics; o app reenvia o que não
    # recebeu 2xx.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def applied_versions(conn: sqlite3.Connection) -> List[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [int(r[0]) for r in rows]


def init_db(path: str) -> None:
    """Cria o arquivo, liga WAL e aplica migrações pendentes (idempotente)."""
    conn = _open(path)
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal" and path != ":memory:":
            raise RuntimeError("SQLite recusou journal_mode=WAL (modo atual: %s)" % mode)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY NOT NULL,"
            " applied_at TEXT NOT NULL)"
        )
        done = set(applied_versions(conn))
        for version, statements in MIGRATIONS:
            if version in done:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def ensure_db(path: str) -> None:
    """init_db uma vez por caminho neste processo."""
    key = os.path.abspath(path)
    if key in _initialized:
        return
    with _init_lock:
        if key in _initialized:
            return
        init_db(path)
        _initialized.add(key)


@contextmanager
def connect(path: str) -> Iterator[sqlite3.Connection]:
    ensure_db(path)
    conn = _open(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    # IMMEDIATE pega o lock de escrita já no início; com busy_timeout, dois
    # uploads simultâneos esperam em fila em vez de falhar no meio.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
