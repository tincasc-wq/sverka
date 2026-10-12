import httpx

from app.core.config import get_settings

MAX_BODY_BYTES = 5_000_000


def make_client(proxy: str | None = None) -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.http_timeout_s,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
        proxy=proxy or None,
    )


async def fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """GET с ограничением размера ответа (раздел 9: лимит скачивания)."""
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise ValueError(f"response larger than {MAX_BODY_BYTES} bytes: {url}")
            chunks.append(chunk)
        return b"".join(chunks)
