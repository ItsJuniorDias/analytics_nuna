"""Autenticação: chave do app na ingestão e token de admin nas estatísticas."""

import hmac
from typing import Iterable, Optional

from fastapi import HTTPException, Request

from .config import Settings


def _matches_any(candidate: Optional[str], secrets: Iterable[str]) -> bool:
    if not candidate:
        return False
    given = candidate.encode("utf-8")
    ok = False
    # Compara com todas as chaves sem sair no primeiro acerto, e em tempo
    # constante por chave, para não vazar por tempo qual chave (ou prefixo) bateu.
    for secret in secrets:
        if hmac.compare_digest(given, secret.encode("utf-8")):
            ok = True
    return ok


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def require_app_key(request: Request) -> None:
    """X-Nuna-Key. A chave vai dentro do binário do app, então não é segredo
    de verdade: serve para barrar tráfego aleatório e permitir rotação, não
    para provar que o evento veio de um aparelho legítimo."""
    settings = _settings(request)
    if not _matches_any(request.headers.get("x-nuna-key"), settings.app_keys):
        raise HTTPException(status_code=401, detail="invalid_app_key")


def require_admin(request: Request) -> None:
    settings = _settings(request)
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    valid = (
        settings.admin_token is not None
        and scheme.lower() == "bearer"
        and _matches_any(token.strip(), (settings.admin_token,))
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="invalid_admin_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
