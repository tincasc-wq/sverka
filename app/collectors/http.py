import asyncio
import ipaddress
import socket

import httpx

from app.core.config import get_settings

MAX_BODY_BYTES = 5_000_000


class UnsafeUrl(ValueError):
    """Ссылка ведёт на внутренний адрес — защита от SSRF (раздел 9)."""


async def assert_public_host(host: str) -> None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"cannot resolve {host}") from exc
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise UnsafeUrl(f"{host} resolves to non-public address {ip}")


async def _guard_request(request: httpx.Request) -> None:
    if request.url.scheme not in ("http", "https"):
        raise UnsafeUrl(f"scheme not allowed: {request.url.scheme}")
    await assert_public_host(request.url.host)


def make_client(
    proxy: str | None = None, *, public_only: bool = False, ipv6: bool = False
) -> httpx.AsyncClient:
    """public_only — для ссылок от пользователей: каждый запрос, включая редиректы, проверяется.

    ipv6 — соединяться только по IPv6 (если нет прокси): с сервера в РФ t.me по IPv4 не
    отвечает, а система в контейнере сначала пробует IPv4 и ждёт таймаута (13.10.2026).
    """
    settings = get_settings()
    transport = httpx.AsyncHTTPTransport(local_address="::") if ipv6 and not proxy else None
    return httpx.AsyncClient(
        transport=transport,
        timeout=settings.http_timeout_s,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
        proxy=proxy or None,
        event_hooks={"request": [_guard_request]} if public_only else None,
    )


async def fetch(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> tuple[bytes, httpx.URL]:
    """GET с лимитом размера ответа (раздел 9). Возвращает тело и итоговый URL после редиректов."""
    async with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise ValueError(f"response larger than {MAX_BODY_BYTES} bytes: {url}")
            chunks.append(chunk)
        return b"".join(chunks), response.url


async def fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    body, _ = await fetch(client, url)
    return body
