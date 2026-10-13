"""AI CICD Security Guardrail service package."""


def create_app():
    """Import the web stack lazily so configuration utilities stay lightweight."""
    from .app import create_app as factory

    return factory()


__all__ = ["create_app"]
