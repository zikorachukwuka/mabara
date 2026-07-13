"""Project context: the target repo's CLAUDE.md, and Mabara's own
per-repo session notes.

Mabara deliberately does NOT let the SDK load the target repo's .claude
settings (see setting_sources in voice_agent.py): a repo's settings.json
can define hooks — shell commands that run on tool events — and permission
allow-rules, and an untrusted repo must never get either past the voice
gate. The repo's conventions still matter, so its CLAUDE.md files are read
here as data and appended to the system prompt: prose that informs, never
configuration that executes.

Session notes are the other half of the trust split: CLAUDE.md is the
HUMAN'S instruction file, which the agent must never edit (a self-editable
instruction channel defeats the approval model); the notes file is the
AGENT'S notebook, which lives outside every repo (in Mabara's data dir,
keyed by repo path like the locks) and which the human can read anytime.
"""

import hashlib
import os

from . import config

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


# ---------- Mabara's per-repo session notes ----------

NOTES_DIR = os.path.join(config.DATA_DIR, "notes")
# Notes ride in the system prompt every session — bound them hard.
REPO_NOTES_MAX_CHARS = 8_000


def repo_notes_path(repo_path):
    """One notes file per repo, keyed like the repo locks: hash of the
    normalized path, never anything inside the repo itself."""
    digest = hashlib.sha256(
        os.path.normcase(os.path.abspath(repo_path)).encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(NOTES_DIR, digest + ".md")


def load_repo_notes(repo_path):
    """The agent's saved notes for this repo, or ""."""
    try:
        with open(repo_notes_path(repo_path), encoding="utf-8") as f:
            return f.read().strip()[:REPO_NOTES_MAX_CHARS]
    except OSError:
        return ""


def save_repo_notes(repo_path, text):
    """Replace the notes file. Returns True when the text was clipped to
    the cap — the caller tells the model so it can tighten next time."""
    os.makedirs(NOTES_DIR, exist_ok=True)
    clipped = len(text) > REPO_NOTES_MAX_CHARS
    if clipped:
        text = text[:REPO_NOTES_MAX_CHARS]
    with open(repo_notes_path(repo_path), "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return clipped
