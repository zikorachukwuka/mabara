"""Project context: the target repo's CLAUDE.md, loaded as plain text.

Mabara deliberately does NOT let the SDK load the target repo's .claude
settings (see setting_sources in voice_agent.py): a repo's settings.json
can define hooks — shell commands that run on tool events — and permission
allow-rules, and an untrusted repo must never get either past the voice
gate. The repo's conventions still matter, so its CLAUDE.md files are read
here as data and appended to the system prompt: prose that informs, never
configuration that executes.
"""

import os

# Enough for any sane CLAUDE.md; a repo shipping a book gets clipped so a
# hostile file can't crowd the system prompt out of the model's context.
PROJECT_NOTES_MAX_CHARS = 20_000

# Root-level only — the standard places humans put project instructions.
PROJECT_NOTES_FILES = ("CLAUDE.md", "CLAUDE.local.md")


def project_notes(repo_path):
    """The repo's CLAUDE.md (+ CLAUDE.local.md) as one labeled string, or
    "" when neither exists or both are empty."""
    parts = []
    for name in PROJECT_NOTES_FILES:
        path = os.path.join(repo_path, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip()
        except OSError:
            continue
        if content:
            parts.append(f"--- {name} ---\n{content}")
    notes = "\n\n".join(parts)
    if len(notes) > PROJECT_NOTES_MAX_CHARS:
        notes = (notes[:PROJECT_NOTES_MAX_CHARS]
                 + "\n[... clipped — the file continues on disk]")
    return notes
