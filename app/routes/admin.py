"""DELETE /v1/admin/events: botão "Limpar eventos" do painel.

Apaga todos os eventos e os totais diários, de todos os períodos. Serve para
zerar o painel depois de testes (build Debug com a flag de DEV ligada). Não tem
volta: o painel pede para digitar LIMPAR antes de chamar.
"""

from typing import Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from .. import db
from ..auth import require_admin
from ..config import Settings

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def clear_events(db_path: str) -> Tuple[int, int]:
    with db.connect(db_path) as conn:
        with db.write_transaction(conn):
            # Conta antes: DELETE sem WHERE pode não informar quantas linhas
            # saíram, dependendo da versão do SQLite.
            events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            daily = int(conn.execute("SELECT COUNT(*) FROM daily_event_counts").fetchone()[0])
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM daily_event_counts")
        # Em WAL, o conteúdo apagado continua no arquivo -wal até um
        # checkpoint. TRUNCATE leva tudo para o banco e zera o -wal.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return events, daily


@router.delete("/events", summary="Apaga todos os eventos e os totais diários")
async def delete_all_events(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    events, daily = await run_in_threadpool(clear_events, settings.db_path)
    return {"deleted_events": events, "deleted_daily_counts": daily}
