"""Все модели импортируются здесь, чтобы Alembic видел полную схему."""

from app.models.accounts import Feedback, User
from app.models.archive import Derivation, Document, DocumentVersion, Story, StoryDocument
from app.models.base import Base
from app.models.checks import Check, Claim, ClaimEvidence
from app.models.llm import LlmCall
from app.models.orgs import Organization, OrgInvite, OrgMember
from app.models.registry import Outcome, Source, SourceScore

__all__ = [
    "Base",
    "Check",
    "Claim",
    "ClaimEvidence",
    "Derivation",
    "Document",
    "DocumentVersion",
    "Feedback",
    "LlmCall",
    "OrgInvite",
    "OrgMember",
    "Organization",
    "Outcome",
    "Source",
    "SourceScore",
    "Story",
    "StoryDocument",
    "User",
]
