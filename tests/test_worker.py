"""Перезапуск воркера: прерванные задачи не должны висеть до истечения таймаута."""

from app.workers.main import clear_stale_in_progress


class FakeRedis:
    def __init__(self, keys: list[str]) -> None:
        self.keys = set(keys)

    async def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        for key in sorted(self.keys):
            if key.startswith(prefix):
                yield key

    async def delete(self, *keys: str) -> None:
        self.keys -= set(keys)


async def test_restart_clears_only_in_progress_marks() -> None:
    redis = FakeRedis(
        ["arq:in-progress:abc", "arq:in-progress:cron:x:1", "arq:job:abc", "arq:queue"]
    )
    assert await clear_stale_in_progress(redis) == 2
    assert redis.keys == {"arq:job:abc", "arq:queue"}  # сами задачи и очередь на месте
    assert await clear_stale_in_progress(redis) == 0
