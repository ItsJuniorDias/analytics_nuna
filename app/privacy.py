"""Middleware que esconde do app o endereço e a identificação do cliente.

O IP chega ao servidor porque é assim que TCP funciona; o compromisso da
política (e a exceção de "internal operations" do COPPA) é não guardar, não
logar e não usar. A forma mais segura de cumprir isso é o código da aplicação
nunca ver o dado: este middleware apaga scope["client"] e os cabeçalhos que
carregam IP ou User-Agent antes de qualquer rota, handler de erro ou log.

O access log do uvicorn fica fora do alcance de um middleware (é escrito pelo
servidor), por isso ele é desligado com --no-access-log em todos os comandos
de execução, e --no-proxy-headers impede o uvicorn de copiar X-Forwarded-For
para o client.
"""

from typing import Awaitable, Callable, Iterable, MutableMapping, Tuple

Scope = MutableMapping[str, object]
Message = MutableMapping[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Cabeçalhos com IP (direto ou repassado por proxy/CDN), User-Agent e
# qualquer coisa que funcione como identificador entre requisições.
DROPPED_HEADERS = frozenset(
    {
        b"user-agent",
        b"x-forwarded-for",
        b"x-real-ip",
        b"forwarded",
        b"true-client-ip",
        b"cf-connecting-ip",
        b"cf-connecting-ipv6",
        b"fastly-client-ip",
        b"x-client-ip",
        b"x-cluster-client-ip",
        b"x-original-forwarded-for",
        b"x-appengine-user-ip",
        b"x-envoy-external-address",
        b"via",
        b"referer",
        b"cookie",
    }
)


def _filter_headers(headers: Iterable[Tuple[bytes, bytes]]) -> list:
    return [(k, v) for (k, v) in headers if k.lower() not in DROPPED_HEADERS]


class PrivacyScrubMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") in ("http", "websocket"):
            scope = dict(scope)
            scope["client"] = None
            scope["headers"] = _filter_headers(scope.get("headers") or [])  # type: ignore[arg-type]
        await self.app(scope, receive, send)


# Painel: script e estilo servidos pelo próprio app, sem CDN nem inline, então
# a CSP pode proibir todo o resto (inclusive enviar dados para outro domínio).
DASHBOARD_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")

        async def send_with_headers(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])  # type: ignore[call-overload]
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"no-referrer"))
                # "/" é o painel; "/dashboard/..." são os arquivos dele.
                if path == "/" or path.startswith("/dashboard"):
                    headers.append((b"content-security-policy", DASHBOARD_CSP.encode("ascii")))
                    headers.append((b"cache-control", b"no-store"))
                elif path.startswith("/v1/stats"):
                    # Estatísticas nunca em cache de navegador ou proxy.
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
