"""Middlewares ASGI de seguridad (sin BaseHTTPMiddleware, para no añadir latencia por petición)."""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# La interfaz solo carga scripts y estilos propios (sin handlers ni scripts inline).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class SameOriginMiddleware:
    """Rechaza con 403 las peticiones que modifican estado si el Origin del navegador no coincide con el Host."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k: v for k, v in scope["headers"] if k in (b"origin", b"host")}
            origin = headers.get(b"origin")
            if origin and urlsplit(origin.decode("latin-1")).netloc != headers.get(b"host", b"").decode("latin-1"):
                await JSONResponse({"detail": "Origen no permitido"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """Responde 413 si el cuerpo supera `max_bytes` (por Content-Length o contando lo recibido)."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        too_large = JSONResponse({"detail": "Cuerpo de la petición demasiado grande"}, status_code=413)
        for k, v in scope["headers"]:
            if k == b"content-length":
                try:
                    if int(v) > self.max_bytes:
                        await too_large(scope, receive, send)
                        return
                except ValueError:
                    pass
                break

        received = 0
        started = False

        class _Exceeded(BaseException):
            """BaseException para que no la capturen los `except Exception` de los endpoints."""

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > self.max_bytes:
                    raise _Exceeded
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _Exceeded:
            if not started:
                await too_large(scope, receive, send)


class SecurityHeadersMiddleware:
    """Añade CSP, X-Frame-Options, nosniff y Referrer-Policy a todas las respuestas."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] = [*msg.get("headers", []), *SECURITY_HEADERS]
            await send(msg)

        await self.app(scope, receive, send_with_headers)


def webhook_token_ok(headers, query, expected: str) -> tuple[bool, str]:
    """Valida el token del webhook (cabecera `Authorization: Bearer` / `X-Dedalo-Token` o `?token=`).

    Devuelve (válido, origen) para poder quitar el token del payload que ve el flujo.
    """
    auth = headers.get("authorization", "")
    candidates = [
        ("authorization", auth[7:] if auth.lower().startswith("bearer ") else ""),
        ("x-dedalo-token", headers.get("x-dedalo-token", "")),
        ("query", query.get("token", "")),
    ]
    for source, value in candidates:
        if value and hmac.compare_digest(value.encode(), expected.encode()):
            return True, source
    return False, ""
