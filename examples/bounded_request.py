"""Live verification helper: cap parent response size without changing user settings."""

from aelix_agent_core.harness.hooks import BeforeProviderPayloadResult


def setup(aelix):
    def bounded(event, _ctx):
        if not isinstance(event.payload, dict):
            return None
        payload = dict(event.payload)
        field = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
        payload[field] = min(payload.get(field) or 2048, 2048)
        return BeforeProviderPayloadResult(payload=payload)

    aelix.on("before_provider_payload", bounded)
