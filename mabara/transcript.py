"""The spoken transcript log and per-repo session persistence."""

import json
import os
import time

from . import config
from .terminal import accent, dim, DOT

# Running line count of TRANSCRIPT_FILE, so each entry can be referenced as
# a clickable file:line in the terminal. Counted once lazily, then kept in
# step by counting the newlines actually written; rotation resets it.
_transcript_lines = None


def _count_file_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def append_transcript(role, text):
    """The terminal only shows subtitles while Mabara speaks; the full prose
    lands here so it can always be re-read. Returns the 1-based line number
    where the entry starts, or None if the write failed."""
    global _transcript_lines
    if not text:
        return None
    try:
        if (os.path.exists(config.TRANSCRIPT_FILE)
                and os.path.getsize(config.TRANSCRIPT_FILE)
                >= config.TRANSCRIPT_MAX_BYTES):
            os.replace(config.TRANSCRIPT_FILE, config.TRANSCRIPT_FILE + ".1")
            _transcript_lines = 0
        if _transcript_lines is None:
            _transcript_lines = _count_file_lines(config.TRANSCRIPT_FILE)
        entry = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {role}: {text}\n"
        with open(config.TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        first_line = _transcript_lines + 1
        # Code blocks carry their own newlines, so an entry can span lines
        _transcript_lines += entry.count("\n")
        return first_line
    except OSError:
        return None


# ---------- Session persistence ----------

def load_sessions():
    if not os.path.exists(config.SESSION_STORE_FILE):
        return {}
    try:
        with open(config.SESSION_STORE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_session(repo_path, session_id):
    sessions = load_sessions()
    sessions[repo_path] = session_id
    # Write-then-rename: a crash mid-write must not corrupt the store —
    # load_sessions answers a corrupt file by silently forgetting every
    # saved conversation.
    tmp_file = config.SESSION_STORE_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(sessions, f, indent=2)
    os.replace(tmp_file, config.SESSION_STORE_FILE)


def prompt_resume(session_id):
    """Enter defaults to yes — if you're being asked at all, you were in the
    middle of something on this repo last time.

    The question is printed via sys.stdout and input() is called BARE:
    input(prompt) hands the prompt to the C runtime's console readline
    path rather than sys.stdout, and on this setup that write never
    reached the screen — the app sat waiting on an invisible question."""
    print(f"  {accent(DOT)} pick up where you left off? {dim('(Y/n)')} ",
          end="", flush=True)
    try:
        answer = input()
    except EOFError:
        return False
    return not answer.strip().lower().startswith("n")
