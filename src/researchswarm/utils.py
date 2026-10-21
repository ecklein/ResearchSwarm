"""Small shared utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def generate_id(prefix: str) -> str:
    """Return a readable, globally unique identifier."""
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(UTC)


def ensure_aware(value: datetime, field_name: str) -> None:
    """Validate that a datetime has timezone information."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
