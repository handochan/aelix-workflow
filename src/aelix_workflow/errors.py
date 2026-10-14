"""The single refusal type shared by graph, schema and condition validation."""


class WorkflowError(ValueError):
    """A rejected workflow operation; no partial graph mutation is committed."""
