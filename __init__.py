"""Kapso Hermes platform plugin package."""

try:
    from .adapter import register
except ImportError:  # Allows direct pytest collection from the plugin root.
    from adapter import register

__all__ = ["register"]
