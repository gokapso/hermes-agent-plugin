"""Kapso Hermes platform plugin package."""

from pathlib import Path

try:
    from .adapter import register as register_platform
    from .xquik_tool import XQUIK_SEARCH, xquik_search
except ImportError:  # Allows direct pytest collection from the plugin root.
    from adapter import register as register_platform
    from xquik_tool import XQUIK_SEARCH, xquik_search


def register(ctx) -> None:
    """Register the Kapso platform and optional Xquik research tools."""
    ctx.register_tool(
        name="xquik_search",
        toolset="xquik",
        schema=XQUIK_SEARCH,
        handler=xquik_search,
        requires_env=["XQUIK_API_KEY"],
        description="Search public X posts through Xquik. Not affiliated with X Corp.",
        emoji="X",
    )
    ctx.register_skill(
        "xquik-social-research",
        Path(__file__).parent / "skills" / "xquik-social-research" / "SKILL.md",
        description="Research current public X posts with Xquik.",
    )
    register_platform(ctx)


__all__ = ["register"]
