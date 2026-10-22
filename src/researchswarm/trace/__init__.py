"""Public portable trace, validation and event-replay interfaces."""

from researchswarm.trace.common import (
    SCHEMA_VERSION,
    TraceValidationError,
    UnsupportedTraceVersionError,
    ValidationResult,
    fingerprint_json,
)
from researchswarm.trace.comparison import compare_traces
from researchswarm.trace.models import (
    ArtifactTrace,
    ResearchEntityTrace,
    RunTraceRun,
    TaskTrace,
    TraceExportOptions,
    UsageSummary,
)
from researchswarm.trace.replay import EventReducer, ReplayEngine, ReplayState, verify_replay
from researchswarm.trace.trace import RunTrace
from researchswarm.trace.validation import TraceValidator

__all__ = [
    "SCHEMA_VERSION",
    "UnsupportedTraceVersionError",
    "TraceValidationError",
    "ValidationResult",
    "fingerprint_json",
    "RunTraceRun",
    "TaskTrace",
    "ArtifactTrace",
    "ResearchEntityTrace",
    "TraceExportOptions",
    "UsageSummary",
    "ReplayEngine",
    "ReplayState",
    "EventReducer",
    "verify_replay",
    "compare_traces",
    "TraceValidator",
    "RunTrace",
]
