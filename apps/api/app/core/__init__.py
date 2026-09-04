"""Core configuration and infrastructure package.

A0 owns settings and logging configuration.
"""

from app.core.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
