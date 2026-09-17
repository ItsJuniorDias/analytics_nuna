"""python -m app: sobe o servidor com as opções de privacidade já aplicadas.

Equivale a:
    uvicorn app.main:app --host $HOST --port $PORT --no-access-log --no-proxy-headers --no-server-header
"""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        workers=1,  # SQLite + limpeza agendada: um processo só
        access_log=False,  # o access log grava o IP do cliente
        proxy_headers=False,  # não copiar X-Forwarded-For para o client
        server_header=False,
    )


if __name__ == "__main__":
    main()
