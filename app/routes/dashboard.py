"""GET /dashboard: painel estático (HTML + JS puro, sem CDN).

A página em si não tem dado nenhum; ela pede o token de admin e chama as rotas
/v1/stats do mesmo domínio. Por isso pode ser servida sem autenticação.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Lista fechada em vez de StaticFiles: nada de caminho vindo da URL chegando ao
# sistema de arquivos.
ASSETS = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}

router = APIRouter(include_in_schema=False)


@router.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html; charset=utf-8")


@router.get("/dashboard/{asset}")
def dashboard_asset(asset: str) -> FileResponse:
    media_type = ASSETS.get(asset)
    if media_type is None:
        raise HTTPException(status_code=404, detail="not_found")
    return FileResponse(STATIC_DIR / asset, media_type=media_type)
