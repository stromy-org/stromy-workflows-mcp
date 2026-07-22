"""Example resource — replace with your own."""

from fastmcp.resources import resource


@resource("config://example")
def example_config() -> str:
    """Return a small example configuration document."""
    return '{"server": "Stromy Workflows MCP", "version": "0.1.0"}'
