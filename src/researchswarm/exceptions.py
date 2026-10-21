"""Public exception hierarchy for ResearchSwarm."""


class ResearchSwarmError(Exception):
    """Base class for all framework errors."""


class CycleError(ResearchSwarmError):
    """Raised when a dependency would make the task graph cyclic."""


class TaskNotFoundError(ResearchSwarmError):
    """Raised when a task cannot be found."""


class RunNotFoundError(ResearchSwarmError):
    """Raised when a run cannot be found."""


class ArtifactNotFoundError(ResearchSwarmError):
    """Raised when an artifact cannot be found."""


class AgentNotFoundError(ResearchSwarmError):
    """Raised when a task references an unregistered agent type."""


class StorageError(ResearchSwarmError):
    """Raised when a storage operation fails."""


class SerializationError(ResearchSwarmError):
    """Raised when structured data cannot be serialized safely."""


class InvalidStateTransitionError(ResearchSwarmError):
    """Raised when a model is moved through an invalid state transition."""


class DuplicateTaskError(ResearchSwarmError):
    """Raised when a task identifier is already present in a graph."""


class InvalidTaskError(ResearchSwarmError):
    """Raised when a task or its dependencies are invalid."""

