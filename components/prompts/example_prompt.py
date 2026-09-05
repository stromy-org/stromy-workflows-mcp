"""Example prompt — replace with your own."""

from fastmcp.prompts import prompt


@prompt
def code_review(code: str, language: str = "python") -> str:
    """Generate a code review prompt for the supplied snippet."""
    return f"Please review this {language} code:\n\n```{language}\n{code}\n```"
