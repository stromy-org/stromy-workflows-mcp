"""Example tool — replace with your own."""

from fastmcp.tools import tool


@tool
def echo(message: str) -> str:
    """Echo a message back to the caller."""
    return message
