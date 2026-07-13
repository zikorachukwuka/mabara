"""Mutable runtime state shared across modules — the one place it lives.

Every module reads and writes these as attributes (`state.repo_root`),
never via `from state import repo_root` copies, so an assignment made by
one module (or a test's monkeypatch) is seen by all of them.
"""

import asyncio

# The Claude Agent SDK costs ~1.4s to import — part of the blank-terminal
# time before the banner's first frame. main() loads it in a background
# thread (alongside the STT/TTS models) while the banner draws; nothing
# touches these names until the loader has finished.
ClaudeSDKClient = None
ClaudeAgentOptions = None
AgentDefinition = None
PermissionResultAllow = None
PermissionResultDeny = None
StreamEvent = None


def _load_sdk():
    global ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition, \
        PermissionResultAllow, PermissionResultDeny, StreamEvent
    import claude_agent_sdk as sdk
    ClaudeSDKClient = sdk.ClaudeSDKClient
    ClaudeAgentOptions = sdk.ClaudeAgentOptions
    AgentDefinition = sdk.AgentDefinition
    PermissionResultAllow = sdk.PermissionResultAllow
    PermissionResultDeny = sdk.PermissionResultDeny
    StreamEvent = sdk.StreamEvent


# Set once at startup in main(), used by the permission callback
stt = None
speaker = None
recorder = None
git_safety = None
readonly_mode = False
debug_mode = False
# Absolute path of the repo this session talks about. Auto-approved reads are
# confined to it; None (before main() sets it) means nothing auto-approves.
repo_root = None

# Seconds from query to Claude's first text delta, set by ask_claude each
# turn — the number that separates "model is slow" from "audio is slow"
_first_token_secs = None

# Whether this conversation was saved for resume; read by the Ctrl+C handler
session_saved = False

# The user's trusted fetch domains (data/allowed-domains.txt), loaded once
# at startup — fetches anywhere else need a spoken yes, per domain.
web_allowlist = frozenset()

# Count of in-process tools currently executing something slow (a test
# run), so the stall watcher doesn't call honest work a hang.
_tool_busy = 0

# Count of voice approvals in flight or queued, so the barge-in watcher
# doesn't mistake an answer for "cut Claude off". Parallel tool calls make
# the permission callback re-enter concurrently; a plain boolean here let
# the first approval's cleanup unpause the watcher while a second was still
# listening — the user's next press then became a brand-new task.
_approvals_pending = 0

# Serializes the spoken ask itself: one question, one mic, one answer at a
# time. Without it, concurrent callbacks raced record_while_held on the
# shared Recorder and clobbered each other's frames — an approval could be
# denied with "no answer" before the user had any chance to give one.
_approval_lock = asyncio.Lock()

# Queued asks per tool name, so the spoken question can offer "yes to all"
# when more calls of the same tool are waiting behind the current one.
_pending_asks = {}

# Whole-task grants, cleared when the task ends. "Yes for the whole task"
# during an Edit/Write approval adds "edits" (covers both, repo-confined);
# during any other tool's approval it adds that tool's name, so a task that
# needs five web searches needs one yes, not five. Bash never rides a grant.
_task_grants = set()

# The approved plan's file set (normalized absolute paths) — unlike
# _task_grants this SURVIVES turn boundaries, because plans legitimately
# span turns (interruptions, questions, usage-limit blips). Observed live
# 2026-07-13: the plan was approved, a limit error and two questions
# passed, and by the time the worker executed, the turn-scoped grant was
# gone — every write voice-asked and the user's questions were eaten as
# denials. Scope is the tradeoff for persistence: ONLY the files the
# user heard named in the plan, still repo-confined, diffs still print.
# Cleared by: a newly approved plan (replaces), 'revert that', 'commit
# this', or session end.
plan_files = frozenset()
