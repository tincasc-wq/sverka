"""Все модели импортируются здесь, чтобы Alembic видел полную схему."""

from app.models.accounts import Feedback, LoginToken, PlanRequest, User
from app.models.archive import Derivation, Document, DocumentVersion, Story, StoryDocument
from app.models.base import Base
from app.models.checks import Check, Claim, ClaimEvidence, Watch, WatchSubscriber
from app.models.llm import LlmCall
from app.models.orgs import ApiKey, Organization, OrgInvite, OrgMember
from app.models.registry import Outcome, Source, SourceScore
from app.models.topics import Topic, TopicHit, TopicSubscriber

__all__ = [
    "ApiKey",
    "Base",
    "Check",
    "Claim",
    "ClaimEvidence",
    "Derivation",
    "Document",
    "DocumentVersion",
    "Feedback",
    "LlmCall",
    "LoginToken",
    "OrgInvite",
    "OrgMember",
    "Organization",
    "PlanRequest",
    "Outcome",
    "Source",
    "SourceScore",
    "Story",
    "StoryDocument",
    "Topic",
    "TopicHit",
    "TopicSubscriber",
    "User",
    "Watch",
    "WatchSubscriber",
]
