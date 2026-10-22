"""Versioning, canonical JSON and diagnostics for the public trace boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from researchswarm.exceptions import ResearchSwarmError
from researchswarm.utils import ensure_aware

SCHEMA_VERSION = "researchswarm.trace.v1"


class UnsupportedTraceVersionError(ResearchSwarmError):
    """The trace uses an interchange version this installation cannot read."""


class TraceValidationError(ResearchSwarmError):
    """Malformed trace data or an invalid event stream."""


def check_version(value: str) -> None:
    if value != SCHEMA_VERSION:
        raise UnsupportedTraceVersionError(f"unsupported trace version: {value!r}")


def canonical_json(value: Any) -> str:
    """Canonical for this format: UTF-8, sorted string keys, no whitespace/NaN.

    Numeric 1 and 1.0 intentionally remain different JSON representations.
    This is a local format convention, not an implementation of RFC 8785.
    """

    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)

    check(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint_json(value: Any) -> str:
    """SHA-256 comparison fingerprint; not authenticated or tamper-proof provenance."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    ensure_aware(value, "timestamp")
    return value.isoformat()


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    ensure_aware(parsed, "timestamp")
    return parsed


def optional_timestamp(value: Any) -> datetime | None:
    return None if value is None else parse_timestamp(value)


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise TraceValidationError("; ".join(self.errors))
