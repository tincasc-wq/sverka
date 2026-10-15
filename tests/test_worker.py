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


async def test_backlog_alert_once_per_window(session, monkeypatch) -> None:
    from app import embeddings
    from app.models import Document
    from app.web import notify
    from app.workers.main import alert_on_backlog

    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return True

    class Redis:
        def __init__(self) -> None:
            self.keys: set[str] = set()

        async def set(self, key, value, ex=None, nx=False):
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    monkeypatch.setattr(notify, "notify_admin", fake_notify)
    monkeypatch.setattr(embeddings, "BACKLOG_ALERT", 2)
    session.add_all([Document(url=f"https://q.test/{i}", text="ждёт вектора") for i in range(3)])
    await session.flush()
    ctx = {"redis": Redis()}

    await alert_on_backlog(ctx, session)
    await alert_on_backlog(ctx, session)  # в течение 6 часов — молчим

    assert len(sent) == 1 and "Без векторов 3 публикаций" in sent[0]
