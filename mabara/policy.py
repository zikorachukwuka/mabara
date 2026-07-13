"""The pure permission policy — the security core, kept free of I/O and
audio so tests can pin every branch. See tests/test_voice_agent.py."""

import os
import re
from urllib.parse import urlsplit

from . import config, state
from .tools import PLAN_TOOL, REPLACE_TOOL, RUN_TESTS_TOOL

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


# Commands whose blast radius exceeds "my changes": repo-wide discards
# and deletes. They still only run with a spoken yes — but the yes must be
# informed, and "git restore ." sounds harmless while quietly destroying
# the USER'S own uncommitted work too (observed live 2026-07-13).
BASH_DESTRUCTIVE_PATTERN = re.compile(
    r"git\s+(?:restore|checkout)\s+(?:--\s+)?\.(?:\s|$)"
    r"|git\s+reset\s+--hard"
    r"|git\s+clean\b"
    r"|rm\s+-[a-z]*[rf]", re.IGNORECASE)


def bash_warning(command):
    """A spoken caution for commands that can destroy work beyond the
    task's own changes, or None. A warning, never a silent deny — the
    user can still say yes, but now they know what they're saying it to."""
    if BASH_DESTRUCTIVE_PATTERN.search(str(command)):
        return ("careful — this can discard or delete work across the "
                "repo, not just my changes")
    return None


# ---------- Web fetches (domains, hygiene, the trusted-domain list) ----------

def url_domain(url):
    """The host of an http(s) URL, lowercased, port and credentials
    stripped — the honest speakable unit of a URL, the way a filename is
    for a path. None for anything that isn't a plain web address."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    try:
        return parts.hostname or None
    except ValueError:
        return None


# An encoded blob riding a URL is the exfiltration shape: injected
# instructions can't run commands here (the voice gate holds), but they can
# try to smuggle what the model has read out through a fetch's address.
_ENCODED_BLOB = re.compile(r"[A-Za-z0-9+/=_\-%]{80,}")
URL_QUERY_MAX_CHARS = 150


def url_flags(url):
    """Human-sentence warnings for exfiltration-shaped URLs. A flagged URL
    never auto-approves — not even on a trusted domain — and the spoken
    ask says why. Flags warn; they never silently deny."""
    url = str(url).strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ["an address that couldn't be parsed"]
    flags = []
    if parts.scheme and parts.scheme not in ("http", "https"):
        flags.append("a non-web address scheme")
    if "@" in parts.netloc:
        flags.append("credentials embedded in the address")
    tail = (parts.query or "") + (parts.fragment or "")
    if len(tail) > URL_QUERY_MAX_CHARS:
        flags.append("an unusually long query string")
    elif _ENCODED_BLOB.search(parts.path + tail):
        flags.append("what looks like an encoded data payload")
    return flags


# One domain per line, # comments — a statement of trust the user edits by
# hand, never the model (it lives outside every repo and outside the tools).
WEB_ALLOWLIST_FILE = os.path.join(config.DATA_DIR, "allowed-domains.txt")

WEB_ALLOWLIST_TEMPLATE = """\
# Domains Mabara may fetch from WITHOUT asking, one per line.
# Only list documentation sites whose authors you trust: fetched pages
# can carry hidden instructions, so a line here is a statement of trust,
# not a convenience. Everything else asks for a spoken yes per domain.
# Lines starting with # are ignored. Examples to uncomment:
# docs.python.org
# developer.mozilla.org
"""


def load_web_allowlist(path=None):
    """The user's trusted fetch domains, or an empty set when the file is
    missing or unreadable — absence fails closed to 'always ask'."""
    try:
        with open(path or WEB_ALLOWLIST_FILE, encoding="utf-8") as f:
            return frozenset(
                line.strip().lower() for line in f
                if line.strip() and not line.strip().startswith("#"))
    except OSError:
        return frozenset()


READONLY_DENY = (
    "This session is read-only: edits and commands are disabled. "
    "Explain or show code instead of changing anything."
)
NO_GIT_DENY = (
    "Edits are disabled: this folder is not a git repository, so "
    "there is no safety net to undo changes. Tell the user that "
    "running git init in this folder enables editing."
)
OTHER_AGENT_DENY = (
    "That agent type is disabled: only the scout (read-only exploration) "
    "and the worker (executes an approved plan) exist here. Use one of "
    "those, or do the work yourself with your own tools in this turn."
)

# The agent types Mabara defines (mabara/agents.py) — the only launches
# the policy lets through. The worker's launch is free like the scout's:
# the launch itself changes nothing, and every mutating call the worker
# makes still lands on this policy under whatever grants are in force.
ALLOWED_AGENT_TYPES = ("scout", "worker")


def permission_decision(tool_name, tool_input, *, readonly, task_grants,
                        git_enabled, web_allowlist=frozenset()):
    """The policy core of voice_permission_callback, kept free of I/O and
    mutable session state so tests can pin every branch of the most
    security-critical decision in the project. task_grants is the set of
    whole-task grants in force ("edits", a tool name, or "WebFetch:<domain>"
    — see state._task_grants); web_allowlist is the user's trusted fetch
    domains. Repo confinement still reads state.repo_root through the
    helpers. Returns one of:
      ("allow", why)    — auto-approve; why in {"read", "bash",
                          "task-grant", "web-allowlist"}
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

    # propose_plan is always free: the tool IS an approval — it speaks the
    # plan and takes the verdict itself; gating it would ask permission to
    # ask permission. It grants nothing until the user says yes into it.
    if tool_name == PLAN_TOOL:
        return ("allow", "plan-tool")

    # Scout launches are free: a scout can only read, glob, and grep, and
    # every inner call it makes is gated here anyway. Any other agent type
    # is refused outright — the primary fence is that no other type is
    # DEFINED (mabara/agents.py), but the CLI has been observed skipping
    # this callback for Task (2026-07-05), so treat this branch as defense
    # in depth, not the lock on the door.
    if tool_name == "Task":
        agent_type = str(tool_input.get("subagent_type", "")).strip().lower()
        if agent_type in ALLOWED_AGENT_TYPES:
            return ("allow", agent_type)
        return ("deny", OTHER_AGENT_DENY)

    # --readonly: a hard no for anything that changes state, regardless of
    # approvals — for exploring repos that must not be touched. Checked
    # before the read-only Bash allowlist so that, as the flag's help
    # promises, no shell command runs at all in a read-only session.
    # run_tests counts as mutating: test suites execute repo code.
    if readonly and tool_name in ("Edit", "Write", "Bash",
                                  RUN_TESTS_TOOL, REPLACE_TOOL):
        return ("deny", READONLY_DENY)

    # replace_text self-gates like propose_plan — it previews and takes
    # its own spoken yes — so outside readonly it passes freely. It also
    # refuses itself without a git checkpoint to revert to.
    if tool_name == REPLACE_TOOL:
        return ("allow", "self-ask")

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

    # Fetches are gated by DOMAIN, not by tool: a "yes to all" given on
    # docs.python.org must not cover an injected redirect to evil.com —
    # the moment a fetched page steers toward a new domain, the grant
    # breaks and a fresh spoken ask names the stranger. A hygiene-flagged
    # URL (exfiltration-shaped) never auto-approves, not even on a trusted
    # domain — it falls to a voice ask that says what's wrong out loud.
    if tool_name == "WebFetch":
        url = str(tool_input.get("url", ""))
        domain = url_domain(url)
        if domain and not url_flags(url):
            if domain in web_allowlist:
                return ("allow", "web-allowlist")
            if f"WebFetch:{domain}" in task_grants:
                return ("allow", "task-grant")
        return ("ask", None)

    # A grant given on any other tool covers only that tool, by name — a
    # yes-to-all on web searches must not leak into anything mutating.
    if tool_name not in ("Bash", "Edit", "Write") and tool_name in task_grants:
        return ("allow", "task-grant")

    return ("ask", None)
