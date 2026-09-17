"""Retenção: apaga eventos brutos antigos e guarda só totais diários.

Uso:
    python -m app.purge --days 180
    python -m app.purge --days 180 --dry-run

A política de privacidade promete apagar eventos individuais depois de 180 dias
e manter só totais sem session_id. Este módulo cumpre as duas partes: antes de
apagar, soma os eventos em daily_event_counts (dia, nome, total).

O serviço web também roda esta limpeza sozinho a cada NUNA_PURGE_INTERVAL_HOURS
(ver PurgeScheduler), porque no Render o disco só é montado no serviço web e
um Cron Job separado não enxergaria o arquivo SQLite.
"""

import argparse
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from . import db
from .catalog import format_timestamp
from .config import ConfigError, Settings

logger = logging.getLogger("nuna.analytics.purge")

# Um evento sai quando o timestamp do cliente OU o dia de chegada é anterior ao
# corte. Pelo dia de chegada, porque o relógio do aparelho pode estar errado;
# pelo timestamp, porque é a data que as estatísticas mostram.
_OLD = "(ts < ? OR received_at < ?)"


@dataclass(frozen=True)
class PurgeResult:
    cutoff: str
    deleted: int
    aggregated_rows: int
    dry_run: bool


def purge(db_path: str, days: int, now: Optional[datetime] = None, dry_run: bool = False) -> PurgeResult:
    if days < 1:
        raise ValueError("days must be >= 1")
    now = now or db.utc_now()
    cutoff = now - timedelta(days=days)
    params = (format_timestamp(cutoff), cutoff.strftime("%Y-%m-%d"))

    with db.connect(db_path) as conn:
        # secure_delete zera as páginas liberadas: "apagado" não fica
        # recuperável no arquivo até ser sobrescrito por acaso.
        conn.execute("PRAGMA secure_delete = ON")
        if dry_run:
            count = conn.execute("SELECT COUNT(*) FROM events WHERE " + _OLD, params).fetchone()[0]
            groups = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM events WHERE " + _OLD
                + " GROUP BY substr(ts, 1, 10), name)",
                params,
            ).fetchone()[0]
            return PurgeResult(format_timestamp(cutoff), int(count), int(groups), True)

        with db.write_transaction(conn):
            aggregated = conn.execute(
                "INSERT INTO daily_event_counts (day, name, events)"
                " SELECT substr(ts, 1, 10) AS day, name, COUNT(*) FROM events WHERE " + _OLD
                + " GROUP BY day, name"
                " ON CONFLICT (day, name) DO UPDATE SET events = events + excluded.events",
                params,
            ).rowcount
            deleted = conn.execute("DELETE FROM events WHERE " + _OLD, params).rowcount
        # Tira as páginas antigas do arquivo WAL também.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    return PurgeResult(format_timestamp(cutoff), int(deleted), int(max(aggregated, 0)), False)


class PurgeScheduler:
    """Roda purge() na subida do serviço e depois a cada interval_hours."""

    def __init__(self, db_path: str, days: int, interval_hours: int) -> None:
        self.db_path = db_path
        self.days = days
        self.interval_seconds = interval_hours * 3600
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._first_run = threading.Event()
        self.last_result: Optional[PurgeResult] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="nuna-purge", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_result = purge(self.db_path, self.days)
                logger.info(
                    "purge: deleted=%d aggregated_rows=%d cutoff=%s",
                    self.last_result.deleted,
                    self.last_result.aggregated_rows,
                    self.last_result.cutoff,
                )
            except Exception:
                logger.exception("purge failed")
            self._first_run.set()
            if self._stop.wait(self.interval_seconds):
                break

    def wait_first_run(self, timeout: float) -> bool:
        return self._first_run.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.purge",
        description="Apaga eventos mais antigos que N dias (guardando totais diários sem session_id).",
    )
    parser.add_argument("--days", type=int, default=None, help="dias de retenção (padrão: NUNA_RETENTION_DAYS, 180)")
    parser.add_argument("--db", default=None, help="caminho do SQLite (padrão: NUNA_DB_PATH)")
    parser.add_argument("--dry-run", action="store_true", help="só conta, não apaga")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        parser.error(str(exc))
        return 2
    days = settings.retention_days if args.days is None else args.days
    if days < 1:
        parser.error("--days must be >= 1")
    db_path = args.db or settings.db_path

    result = purge(db_path, days, dry_run=args.dry_run)
    verb = "would delete" if result.dry_run else "deleted"
    print(
        "%s %d events older than %s (%d daily aggregate rows)"
        % (verb, result.deleted, result.cutoff, result.aggregated_rows)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
