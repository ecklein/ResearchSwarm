"""Factual trace comparison, with no ranking or quality metrics."""

from decimal import Decimal
from typing import Any

from researchswarm.trace.trace import RunTrace


def compare_traces(a: RunTrace, b: RunTrace) -> dict[str, Any]:
    """Differences are b minus a. Missing durations remain None."""
    first, second = a.total_usage(), b.total_usage()
    first_duration, second_duration = a.duration(), b.duration()
    return {
        "run_ids": [a.run.run_id, b.run.run_id],
        "terminal_status": {"a": str(a.run.status), "b": str(b.run.status)},
        "task_count_difference": len(b.tasks) - len(a.tasks),
        "event_count_difference": len(b.events) - len(a.events),
        "artifact_count_difference": len(b.artifacts) - len(a.artifacts),
        "research_entity_count_difference": len(b.research_entities) - len(a.research_entities),
        "cost_difference": float(Decimal(str(second.total_cost)) - Decimal(str(first.total_cost))),
        "token_difference": second.input_tokens
        + second.output_tokens
        - first.input_tokens
        - first.output_tokens,
        "duration_difference": (
            second_duration - first_duration
            if first_duration is not None and second_duration is not None
            else None
        ),
    }
