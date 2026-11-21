"""Flow-Skill domain errors."""


class FlowSkillError(Exception):
    """Base error raised by Flow-Skill."""


class FlowDefinitionError(FlowSkillError):
    """The Python flow definition is invalid."""


class RunNotFoundError(FlowSkillError):
    """The requested workflow run does not exist."""


class TaskNotFoundError(FlowSkillError):
    """The requested task does not exist in the run."""


class InvalidStateError(FlowSkillError):
    """The requested operation is invalid for the current state."""


class FlowVersionMismatchError(FlowSkillError):
    """The persisted run belongs to a different flow version."""


class ValidationError(FlowSkillError):
    """Submitted data is malformed or fails a required validation."""

