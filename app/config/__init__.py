"""Application configuration package.

Loads and exposes the merged configuration from ``settings.yaml`` (defaults)
and environment variable overrides. Consumers should import the singleton
``Settings`` instance from ``app.src.config`` rather than reading this package
directly.
"""
