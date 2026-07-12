"""The pure permission policy — the security core, kept free of I/O and
audio so tests can pin every branch. See tests/test_voice_agent.py."""

import os
import re

from . import state

READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
# NOTE: the CLI runs its own read-only analysis first and auto-approves
# commands it deems safe — including cd+&&-chained compounds — without ever
# consulting can_use_tool (observed live 2026-07-05: `cd repo && git status`
# never reached the callback). This allowlist therefore only governs what
# the CLI would otherwise ask about; it cannot be the *only* line of
# defense for anything (which is why --readonly also disallows Bash at the
# CLI level, in main()).
# git branch is deliberately absent: it creates, force-moves, and deletes
# branches — those are writes, whatever the name suggests.
READ_ONLY_BASH_PREFIXES = (
    "ls", "dir", "cat", "type", "git status", "git log",
    "git diff", "pwd", "echo",
)


# Chaining/redirection operators let a "read-only" prefix smuggle in writes
# (e.g. "cat x; rm -rf ." or "echo hi > file"), and $ lets an auto-approved
# echo expand env vars into the transcript ("echo $AWS_SECRET_ACCESS_KEY")
# or run code ($(...)), so their presence disqualifies a command from
# auto-approval regardless of how it starts.
BASH_UNSAFE_PATTERN = re.compile(r"[;&|><`$\n]")

# git log/diff write files via --output (and -o in some subcommands) with no
# shell redirection involved. A false positive here just falls back to voice
# approval, so match generously.
BASH_WRITE_FLAG_PATTERN = re.compile(r"(^|\s)(-o|--output(-directory)?)(=|\s|$)")


def _path_within(path, root):
    """True if path resolves inside root (case-insensitive drive-safe).
    realpath, not abspath: a symlink committed inside an untrusted repo
    that points at, say, the home directory must not carry repo
    confinement out with it."""
    target = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([target, root]) == root
    except ValueError:  # e.g. paths on different Windows drives
        return False


def _within_repo(path):
    """True if a tool-input path stays inside the session repo. No path at
    all means the tool defaults to the repo cwd, which is fine; before
    main() sets state.repo_root, nothing passes — the check fails closed."""
    if not path:
        return True
    if state.repo_root is None:
        return False
    return _path_within(os.path.expanduser(str(path)), state.repo_root)


def _glob_pattern_prefix(pattern):
    """The static directory prefix of an absolute Glob pattern, or None for
    a relative one (relative patterns root at the tool cwd — the repo).
    Without this, a Glob with an absolute pattern and no path key (e.g.
    C:/Users/**/.ssh/*) would face no repo confinement at all: filenames,
    not contents, but exactly the reconnaissance an injected prompt wants."""
    pattern = os.path.expanduser(str(pattern))
    if not os.path.isabs(pattern):
        return None
    return re.split(r"[*?\[]", pattern)[0] or pattern


def _bash_arg_within_repo(token):
    """Repo confinement for a cat/type argument. Relative paths resolve
    against the repo because the SDK runs Bash with cwd=repo."""
    path = os.path.expanduser(token)
    if not os.path.isabs(path):
        if state.repo_root is None:
            return False
        path = os.path.join(state.repo_root, path)
    return _within_repo(path)


def is_read_only_bash(command):
    """True only for commands that are provably look-don't-touch: an exact
    allowlisted command word (whole-word — 'ls' must not match 'lsfoo'), no
    chaining/redirection, no file-writing flags, and cat/type confined to
    the repo like the Read tool. Anything else falls to voice approval."""
    if BASH_UNSAFE_PATTERN.search(command):
        return False
    command = command.strip()
    lowered = command.lower()
    if BASH_WRITE_FLAG_PATTERN.search(lowered):
        return False
    if not any(lowered == p or lowered.startswith(p + " ")
               for p in READ_ONLY_BASH_PREFIXES):
        return False
    # cat/type print file contents — hold them to the Read tool's rule:
    # only files inside the repo are free to read.
    tokens = command.split()
    if tokens[0].lower() in ("cat", "type"):
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            if not _bash_arg_within_repo(token):
                return False
    return True


READONLY_DENY = (
    "This session is read-only: edits and commands are disabled. "
    "Explain or show code instead of changing anything."
)
NO_GIT_DENY = (
    "Edits are disabled: this folder is not a git repository, so "
    "there is no safety net to undo changes. Tell the user that "
    "running git init in this folder enables editing."
)


def permission_decision(tool_name, tool_input, *, readonly, task_grants,
                        git_enabled):
    """The policy core of voice_permission_callback, kept free of I/O and
    mutable session state so tests can pin every branch of the most
    security-critical decision in the project. task_grants is the set of
    whole-task grants in force ("edits" or a tool name — see
    state._task_grants). Repo confinement still reads state.repo_root
    through the helpers. Returns one of:
      ("allow", why)    — auto-approve; why in {"read", "bash", "task-grant"}
      ("deny", message) — hard block; message goes back to the CLI
      ("ask", None)     — fall through to the spoken approval flow
    """
    # Reads are free — but only inside the session's repo. A prompt-injected
    # instruction in an untrusted repo ("read ~/.ssh/id_rsa") must land on
    # the voice approval, not sail through. Glob is confined through its
    # pattern too: an absolute pattern with no path key is the same probe.
    if tool_name in READ_ONLY_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path")
        if path is None and tool_name == "Glob":
            path = _glob_pattern_prefix(tool_input.get("pattern", ""))
        if _within_repo(path):
            return ("allow", "read")

    # --readonly: a hard no for anything that changes state, regardless of
    # approvals — for exploring repos that must not be touched. Checked
    # before the read-only Bash allowlist so that, as the flag's help
    # promises, no shell command runs at all in a read-only session.
    if readonly and tool_name in ("Edit", "Write", "Bash"):
        return ("deny", READONLY_DENY)

    if tool_name == "Bash" and is_read_only_bash(str(tool_input.get("command", ""))):
        return ("allow", "bash")

    # No git, no edits: without a checkpoint to revert to, a bad edit by
    # voice is unrecoverable. Claude relays this to the user out loud.
    if tool_name in ("Edit", "Write") and not git_enabled:
        return ("deny", NO_GIT_DENY)

    # "Yes, for the whole task" covers remaining edits this turn — but only
    # inside the repo. The grant must not quietly widen into a license to
    # write ~/.bashrc or a startup folder: an out-of-repo target goes back
    # to a voice ask, where describe_action says so out loud. Bash never
    # auto-approves — commands are where the unrecoverable sharp edges are.
    if (tool_name in ("Edit", "Write") and "edits" in task_grants
            and _within_repo(tool_input.get("file_path"))):
        return ("allow", "task-grant")

    # A grant given on any other tool covers only that tool, by name — a
    # yes-to-all on web searches must not leak into anything mutating.
    if tool_name not in ("Bash", "Edit", "Write") and tool_name in task_grants:
        return ("allow", "task-grant")

    return ("ask", None)
