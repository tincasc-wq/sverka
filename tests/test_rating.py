from datetime import UTC, datetime, timedelta

from app.core.scoring import SourceRatingConfig
from app.models.enums import Stance, Verdict
from app.registry.rating import Observation, change_reason, compute_rating, decay

CFG = SourceRatingConfig()
NOW = datetime(2026, 10, 12, tzinfo=UTC)


def obs(verdict, stance=Stance.SUPPORTS, days_ago=0, story=1):
    return Observation(story, NOW - timedelta(days=days_ago), verdict, stance, "История")


def test_new_source_starts_at_prior_with_wide_interval() -> None:
    rating = compute_rating("tg", [], NOW, CFG)

    assert rating.score == 50.0
    assert rating.ci_high - rating.ci_low > 40
    assert not rating.shown(CFG)


def test_confirmed_reports_raise_and_refuted_lower_rating() -> None:
    good = compute_rating("media", [obs(Verdict.CONFIRMED, story=i) for i in range(10)], NOW, CFG)
    bad = compute_rating("media", [obs(Verdict.REFUTED, story=i) for i in range(10)], NOW, CFG)

    assert good.score > 80
    assert bad.score <= 20
    assert good.shown(CFG)
    assert good.ci_high - good.ci_low < 40


def test_debunking_a_fake_counts_in_favour_of_source() -> None:
    debunker = compute_rating("media", [obs(Verdict.REFUTED, Stance.CONTRADICTS)], NOW, CFG)
    spreader = compute_rating("media", [obs(Verdict.REFUTED, Stance.SUPPORTS)], NOW, CFG)

    assert debunker.score > 60 > spreader.score


def test_mixed_unknown_and_unrelated_are_ignored() -> None:
    rating = compute_rating(
        "media",
        [obs(Verdict.MIXED), obs(Verdict.UNKNOWN), obs(Verdict.CONFIRMED, Stance.UNRELATED)],
        NOW,
        CFG,
    )

    assert rating.n_observations == 0
    assert rating.score == 60.0  # априори media 3/2


def test_old_outcomes_weigh_less() -> None:
    assert decay(180, 180) == 0.5
    fresh = compute_rating("tg", [obs(Verdict.REFUTED, days_ago=0)], NOW, CFG)
    old = compute_rating("tg", [obs(Verdict.REFUTED, days_ago=720)], NOW, CFG)

    assert old.score > fresh.score


def test_reason_names_story_and_direction() -> None:
    prev = compute_rating("media", [], NOW, CFG)
    new_obs = [obs(Verdict.REFUTED, story=42)]
    cur = compute_rating("media", new_obs, NOW, CFG)

    reason = change_reason(prev, cur, new_obs, "media", CFG)

    assert reason.startswith("-")
    assert "история #42" in reason
    assert "опровергнута, источник распространял" in reason
    assert "Стартовая оценка" in change_reason(None, prev, [], "media", CFG)
