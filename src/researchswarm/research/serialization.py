"""Explicit JSON serialization with a stable research type discriminator."""

from dataclasses import asdict
from datetime import datetime
from typing import Any, cast

from researchswarm.exceptions import SerializationError
from researchswarm.research.edges import ResearchEdge
from researchswarm.research.models import ENTITY_TYPES, ResearchEntity, entity_type
from researchswarm.serialization import ensure_json


def entity_to_dict(entity: ResearchEntity) -> dict[str, Any]:
    data = asdict(entity)
    data["entity_type"] = entity_type(entity)
    data["created_at"] = entity.created_at.isoformat()
    return cast(dict[str, Any], ensure_json(data))


def entity_from_dict(data: dict[str, Any]) -> ResearchEntity:
    item = ensure_json(data)
    try:
        cls = ENTITY_TYPES[item.pop("entity_type")]
        item["created_at"] = datetime.fromisoformat(item["created_at"])
        return cls(**item)
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(f"invalid research entity: {exc}") from exc


def edge_to_dict(edge: ResearchEdge) -> dict[str, Any]:
    data = asdict(edge)
    data["created_at"] = edge.created_at.isoformat()
    return cast(dict[str, Any], ensure_json(data))


def edge_from_dict(data: dict[str, Any]) -> ResearchEdge:
    item = ensure_json(data)
    try:
        item["created_at"] = datetime.fromisoformat(item["created_at"])
        return ResearchEdge(**item)
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(f"invalid research edge: {exc}") from exc
