"""The voice approval flow and everything it shows: tool-feed lines,
outcome markers, diffs, spill files, the side-by-side review, and the
permission callback the SDK consults before every gated tool call."""

import asyncio
import difflib
import os
import re
import shutil
import subprocess
import time

from . import config, policy, state, transcript
from .commands import grants_whole_task, is_affirmative, is_plain_denial
from .policy import READONLY_DENY, _path_within, _within_repo
from .session import terminal_focus
from .terminal import (
    CHECK, TOOL_MARK, clear_status, cyan, dim, green, red, start_thinking,
    status, stop_thinking, yellow,
)
from .text import speakable


def _short_path(path):
    parts = re.split(r"[\\/]", str(path))
    return "/".join(parts[-2:]) if len(parts) > 1 else str(path)


def _feed_path(path):
    """_short_path, except a path outside the repo shows in full: rendering
    C:/Users/DELL/Documents/project/index.html as 'project/index.html' once
    disguised an out-of-repo probe as a local read."""
    p = str(path)
    if os.path.isabs(p) and state.repo_root and not _path_within(p, state.repo_root):
        return p
    return _short_path(p)


def describe_tool_use(name, tool_input):
    """One dim scrollback line per tool call — the 'work happening' feed."""
    if name == "Read":
        return f"read {_feed_path(tool_input.get('file_path', '?'))}"
    if name == "Edit":
        return f"edit {_feed_path(tool_input.get('file_path', '?'))}"
    if name == "Write":
        return f"write {_feed_path(tool_input.get('file_path', '?'))}"
    if name == "Glob":
        return f"glob {tool_input.get('pattern', '?')}"
    if name == "Grep":
        return f'grep "{tool_input.get("pattern", "?")}"'
    if name == "Bash":
        command = str(tool_input.get("command", "?"))
        return f"run: {command if len(command) <= 56 else command[:55] + '…'}"
    if name == "Task":
        agent = str(tool_input.get("subagent_type", "") or "agent")
        detail = str(tool_input.get("description", "") or tool_input.get("prompt", ""))
        return f"{agent}: {detail[:56]}"
    if name == "WebFetch":
        url = str(tool_input.get("url", "?"))
        return f"fetch {policy.url_domain(url) or url[:56]}"
    if name == "WebSearch":
        query = str(tool_input.get("query", "?"))
        return f'search "{query if len(query) <= 54 else query[:53] + "…"}"'
    if name == policy.PLAN_TOOL:
        return f"propose plan: {str(tool_input.get('goal', ''))[:52]}"
    if name == policy.RUN_TESTS_TOOL:
        return "run tests"
    return name.lower()


# A quiet success marker only when the command was slow enough that the
# silence needed explaining; failures always print.
BASH_OK_MARKER_SECS = 2.5

# Denials already print their own line at deny time — voice denials in the
# approval flow, gate denials (read-only, no-git) right in the callback — so
# a second 'failed' marker for the same event would read as two failures.
# These match the deny messages this module itself sends back to the CLI.
_DENIAL_MARKERS = ("declined", "no answer was captured", "read-only", "disabled")


def _tool_result_text(content):
    """First meaningful line of a tool result, for the failure marker."""
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                content = item.get("text", "")
                break
        else:
            content = ""
    if not isinstance(content, str):
        return ""
    for line in content.splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= 64 else line[:63] + "…"
    return ""


def describe_tool_outcome(block, pending_tools):
    """One dim feed line for a Bash/Edit/Write result: red for failures,
    'ok · Ns' for slow successes — the marker that keeps the feed honest.
    Reads/globs/greps stay silent: exploration probes fail all the time,
    and a red mark on each would drown the real signal."""
    name, started = pending_tools.pop(
        getattr(block, "tool_use_id", None) or "", (None, None))
    if name not in ("Bash", "Edit", "Write"):
        return None
    if getattr(block, "is_error", False):
        detail = _tool_result_text(getattr(block, "content", None))
        if any(marker in detail.lower() for marker in _DENIAL_MARKERS):
            return None
        suffix = f" — {detail}" if detail else ""
        return f"  {red('!')} {dim(f'{name.lower()} failed{suffix}')}"
    took = time.time() - started
    if name == "Bash" and took >= BASH_OK_MARKER_SECS:
        return f"  {dim(f'{TOOL_MARK} ok · {took:.0f}s')}"
    return None


# A spoken command longer than this is noise, not information — the full
# text is on screen. Reading a 100-line heredoc aloud once held a user
# hostage for 75 seconds with the mic closed and Ctrl+C the only way out.
BASH_SPOKEN_MAX = 70


def spoken_command(command):
    """A speakable rendition of a Bash command: its first line, capped."""
    lines = [l.strip() for l in str(command).strip().splitlines() if l.strip()]
    first = lines[0] if lines else "an empty command"
    clipped = len(lines) > 1 or len(first) > BASH_SPOKEN_MAX
    if len(first) > BASH_SPOKEN_MAX:
        first = first[:BASH_SPOKEN_MAX].rstrip() + "…"
    text = f"the command: {first}"
    if clipped:
        text += " — the full command is on your screen"
    return text


def describe_action(tool_name, tool_input, spoken=False):
    if tool_name in ("Edit", "Write"):
        # The spoken question shortens paths to their last component
        # (speakable), so an out-of-repo target gets flagged in words —
        # "bashrc" alone sounds exactly like a local file.
        path = tool_input.get("file_path")
        note = ("" if not path or _within_repo(path)
                else ", which is outside this repo")
        verb = "edit the file" if tool_name == "Edit" else "write to the file"
        return f"{verb} {path or 'unknown file'}{note}"
    if tool_name == "Bash":
        command = tool_input.get("command", "unknown command")
        if spoken:
            return f"run {spoken_command(command)}"
        return f"run the command: {command}"
    if tool_name in policy.READ_ONLY_TOOLS:
        # Only reachable when the target is outside the repo — in-repo
        # reads were auto-approved before the question was ever asked.
        # The pattern fallback covers Glob asked about an absolute pattern.
        target = (tool_input.get("file_path") or tool_input.get("path")
                  or tool_input.get("pattern") or "an unknown path")
        return f"read {target}, which is outside this repo"
    if tool_name == "WebFetch":
        # The domain is the honest speakable unit of a URL — but the full
        # address always prints, and an exfiltration-shaped one (long
        # query, encoded blob, embedded credentials) is called out in
        # words: those are how an injected page tries to smuggle data out.
        url = str(tool_input.get("url", "") or "")
        domain = policy.url_domain(url)
        flags = policy.url_flags(url)
        if spoken:
            action = (f"fetch a page from {domain}" if domain
                      else "fetch a web address I couldn't parse")
            if flags:
                action += " — careful, the address carries " + " and ".join(flags)
            return action + ". The full address is on your screen"
        action = f"fetch the URL: {url or 'unknown address'}"
        if flags:
            action += "  [" + "; ".join(flags) + "]"
        return action
    if tool_name == "WebSearch":
        query = str(tool_input.get("query", "") or "an empty query")
        if spoken and len(query) > 90:
            query = query[:89] + "…"
        return f'search the web for "{query}"'
    if tool_name == policy.RUN_TESTS_TOOL:
        # Only asked when no approved plan covers it — a lone "run the
        # tests?" is the cheapest approval in the whole flow.
        return "run this repo's test suite"
    return f"use the tool {tool_name}"


# Diffs longer than this are truncated on screen — a huge Write must not
# flood a voice-first terminal (the full change still lands in the file).
DIFF_MAX_LINES = 40
# Past this, diffing costs more than the glanceable record is worth.
DIFF_MAX_SOURCE_CHARS = 200_000

# Where a truncated approval preview's full text lands, so the "+N more
# lines" marker can carry a clickable path instead of a dead end. .diff
# gets red/green colorization when the editor opens it; the command file
# is .txt on purpose — a click must never risk running it. Overwritten
# per approval; the file outlives the answer, so what you said yes to
# stays inspectable afterwards.
DIFF_SPILL_FILE = os.path.join(config.DATA_DIR, "last-approval.diff")
COMMAND_SPILL_FILE = os.path.join(config.DATA_DIR, "last-command.txt")


def _spill(path, text):
    """Write a preview's full text for the truncation pointer. Returns the
    path, or None when the write failed (the pointer is simply omitted —
    never block an approval over a bookkeeping file)."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        return path
    except OSError:
        return None


CHECKPOINT_HINT = f"{CHECK} checkpoint saved — say 'revert that' to undo"


def render_diff(tool_name, tool_input):
    """Plain unified-diff lines for a pending Edit/Write, or None when there
    is nothing to show. Edit diffs the two snippets and drops the @@ headers
    — snippet-relative line numbers would be lies; Write diffs the real
    file, so its @@ numbers are kept. Colors are applied at print time."""
    if tool_name == "Edit":
        old = tool_input.get("old_string") or ""
        new = tool_input.get("new_string") or ""
        keep_hunk_headers = False
    elif tool_name == "Write":
        new = tool_input.get("content") or ""
        try:
            with open(tool_input.get("file_path") or "", "r",
                      encoding="utf-8", errors="replace") as f:
                old = f.read()
        except OSError:
            old = ""  # new file: the whole diff is additions
        keep_hunk_headers = True
    else:
        return None
    if old == new:
        return None
    if len(old) > DIFF_MAX_SOURCE_CHARS or len(new) > DIFF_MAX_SOURCE_CHARS:
        return [f"(too large to diff: {len(new.splitlines())} lines)"]
    lines = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            if keep_hunk_headers:
                lines.append(line)
            elif lines:  # separator between hunks, never a lead-in
                lines.append("...")
            continue
        lines.append(line)
    return lines or None


def print_diff(lines, path):
    """Red/green git-style rendering, framed like the code blocks."""
    rule = "-" * 46
    header = f"--[ diff: {_short_path(path)} ]"
    clear_status()
    print(dim(header + rule[len(header):]))
    shown = lines[:DIFF_MAX_LINES]
    for line in shown:
        if line.startswith("+"):
            print(green(line))
        elif line.startswith("-"):
            print(red(line))
        elif line.startswith("@@") or line == "...":
            print(dim(line))
        else:
            print(line)
    if len(lines) > len(shown):
        more = f"... +{len(lines) - len(shown)} more lines"
        spill = _spill(DIFF_SPILL_FILE, "\n".join(lines))
        if spill:
            more += f" {TOOL_MARK} {spill} (ctrl+click to read it all)"
        print(dim(more))
    print(dim(rule))


def print_command(command):
    """Frame a multi-line pending command like the diff blocks, truncated
    the same way — approving from a wall of raw heredoc is not informed
    consent, and it must not scroll the question off the screen."""
    rule = "-" * 46
    header = "--[ command ]"
    clear_status()
    print(dim(header + rule[len(header):]))
    lines = str(command).splitlines()
    for line in lines[:DIFF_MAX_LINES]:
        print(line)
    if len(lines) > DIFF_MAX_LINES:
        more = f"... +{len(lines) - DIFF_MAX_LINES} more lines"
        spill = _spill(COMMAND_SPILL_FILE, str(command))
        if spill:
            more += f" {TOOL_MARK} {spill} (ctrl+click to read it all)"
        print(dim(more))
    print(dim(rule))


# ---------- Side-by-side review (press D during an edit approval) ----------

REVIEW_DIR = os.path.join(config.DATA_DIR, "review")
_code_cli_cache = ("unresolved",)


def _code_cli():
    """Path of the VS Code CLI, or None. Cached — PATH doesn't change
    mid-session, and this runs inside the approval flow."""
    global _code_cli_cache
    if _code_cli_cache == ("unresolved",):
        _code_cli_cache = shutil.which("code")
    return _code_cli_cache


def review_files(tool_name, tool_input):
    """Full current/proposed contents for a pending Edit/Write, or None
    when the outcome can't be reconstructed honestly (e.g. the Edit's
    old_string isn't in the file — the CLI would reject that call anyway).
    Pure: writes nothing, so the prompt hint can probe it safely."""
    path = tool_input.get("file_path") or ""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            current = f.read()
    except OSError:
        current = ""  # new file: the left side is empty
    if tool_name == "Write":
        proposed = tool_input.get("content") or ""
    elif tool_name == "Edit":
        old = tool_input.get("old_string") or ""
        if not old or old not in current:
            return None
        new = tool_input.get("new_string") or ""
        count = -1 if tool_input.get("replace_all") else 1
        proposed = current.replace(old, new, count)
    else:
        return None
    if current == proposed:
        return None
    return current, proposed, (os.path.basename(path) or "file")


def open_review(tool_name, tool_input):
    """Write the pending change as two real files and open them side by
    side in VS Code. The tab is a viewer, not a channel: edits made there
    change nothing — steering happens by voice ("no, <feedback>").
    Returns False when anything is missing or fails."""
    code = _code_cli()
    files = review_files(tool_name, tool_input)
    if not code or not files:
        return False
    current, proposed, name = files  # keep the extension: syntax colors
    try:
        os.makedirs(REVIEW_DIR, exist_ok=True)
        cur_path = os.path.join(REVIEW_DIR, f"current-{name}")
        new_path = os.path.join(REVIEW_DIR, f"proposed-{name}")
        with open(cur_path, "w", encoding="utf-8") as f:
            f.write(current)
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(proposed)
        subprocess.Popen(
            [code, "--diff", cur_path, new_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)  # CREATE_NO_WINDOW: no cmd flash
        return True
    except OSError:
        return False


def poll_review_key(review, prompt):
    """One approval-wait tick: consume pending console keys, open the
    side-by-side on D. Repeats are allowed — reopening a closed tab is
    the reason someone presses it twice."""
    for ch in terminal_focus.take_keys():
        if ch in ("d", "D"):
            clear_status()
            if open_review(*review):
                note = "(side-by-side open in VS Code — click back here to answer)"
            else:
                note = "(couldn't open VS Code — is 'code' on PATH?)"
            print(f"  {dim(note)}")
            status(dim(f"» {prompt}"))
            return


# ---------- Permission callback (auto-approve reads, ask before writes) ----------

async def voice_permission_callback(tool_name, tool_input, context):
    # The folder can BECOME a repo mid-session (the no-git deny message
    # itself says git init fixes the block), so refresh git state before
    # the policy reads it — trusting the startup snapshot once sent the
    # agent around the gate via a shell heredoc.
    if (tool_name in ("Edit", "Write") and not state.readonly_mode
            and not state.git_safety.enabled and state.git_safety.recheck()):
        clear_status()
        print(f"  {dim(f'{CHECK} git repository detected — edits enabled')}")

    def decide():
        return policy.permission_decision(
            tool_name, tool_input, readonly=state.readonly_mode,
            task_grants=state._task_grants,
            git_enabled=state.git_safety.enabled,
            web_allowlist=state.web_allowlist)

    def allow_by_grant():
        # The diff still prints when nobody is asked: an unseen edit
        # is the fastest way to lose the room. Only mutating tools take
        # a checkpoint — a web search has nothing to revert.
        if tool_name in ("Edit", "Write"):
            created = state.git_safety.before_mutation(tool_name, tool_input)
            if created:
                clear_status()
                print(f"  {dim(CHECKPOINT_HINT)}")
            diff = render_diff(tool_name, tool_input)
            if diff:
                print_diff(diff, tool_input.get("file_path", "?"))
                print(f"  {dim('(auto-approved — whole-task grant)')}")
        return state.PermissionResultAllow()

    def deny_by_policy(detail):
        # The denial prints: a blocked edit that leaves no mark looks
        # exactly like a successful one in the tool feed.
        if detail == READONLY_DENY:
            blocked = "blocked (read-only session)"
        elif detail == policy.OTHER_AGENT_DENY:
            blocked = "blocked: only the scout agent exists"
        else:
            blocked = "blocked: not a git repository"
        clear_status()
        print(f"  {red('!')} {dim(describe_tool_use(tool_name, tool_input) + ' — ' + blocked)}")
        return state.PermissionResultDeny(message=detail)

    verdict, detail = decide()
    if verdict == "allow":
        return allow_by_grant() if detail == "task-grant" else state.PermissionResultAllow()
    if verdict == "deny":
        return deny_by_policy(detail)

    # Needs voice approval. Parallel tool calls re-enter this callback
    # concurrently, so the ask itself is serialized: one question, one mic,
    # one answer at a time. The pending counter (not a boolean — every
    # queued ask holds a reference) pauses the barge-in watcher for the
    # whole queue, so an answer is never mistaken for "cut Claude off".
    # Blocking calls run in threads so the SDK's control protocol stays
    # responsive while we talk and listen.
    state._approvals_pending += 1
    state._pending_asks[tool_name] = state._pending_asks.get(tool_name, 0) + 1
    try:
        async with state._approval_lock:
            # Parallel calls arrive as separate control messages a beat
            # apart; a short settle lets the whole batch register in
            # _pending_asks so the question can offer "yes to all" —
            # invisible next to the seconds of TTS synthesis that follow.
            await asyncio.sleep(0.1)
            # An answer given while this call waited in the queue may
            # already cover it — "yes to all" on the first of five web
            # searches approves the other four right here.
            verdict, detail = decide()
            if verdict == "allow":
                return allow_by_grant() if detail == "task-grant" else state.PermissionResultAllow()
            if verdict == "deny":
                return deny_by_policy(detail)

            stop_thinking()
            command = str(tool_input.get("command", "")) if tool_name == "Bash" else ""
            if "\n" in command.strip():
                # Multi-line commands get the framed, truncated treatment —
                # never a raw flood inside the approval banner.
                print(f"\n\n  {yellow('! approval needed')} — Mabara wants to run this command:")
                print_command(command)
            else:
                print(f"\n\n  {yellow('! approval needed')} — Mabara wants to {describe_action(tool_name, tool_input)}")
            # Show the red/green before the yes/no — approving a change you
            # haven't seen is the biggest trust gap a coding tool can have.
            diff = (render_diff(tool_name, tool_input)
                    if tool_name in ("Edit", "Write") else None)
            if diff:
                print_diff(diff, tool_input.get("file_path", "?"))
            question = f"I'd like to {describe_action(tool_name, tool_input, spoken=True)}."
            if diff:
                question += " The diff is on your screen."
            # More of the same tool waiting behind this one? Offer the
            # batch out loud — nobody should learn about 'yes to all' from
            # the README mid-approval-storm.
            queued = state._pending_asks.get(tool_name, 1) - 1
            if queued and tool_name not in ("Bash", "Edit", "Write"):
                more = ("one more like it is" if queued == 1
                        else f"{queued} more like it are")
                question += (f" And {more} waiting — "
                             "you can say yes to all.")
                print(f"  {dim(f'({queued} more {tool_name} queued — say yes to all to approve together)')}")
            # The approval exchange is the most consequential dialogue in a
            # session — it belongs in the transcript like any other speech.
            spoken_question = speakable(question + " Do you approve?")
            transcript.append_transcript("Mabara", spoken_question)
            state.speaker.say(spoken_question)
            # Holding push-to-talk cuts the question short and answers right
            # away — nobody should sit through speech they've already read.
            await asyncio.to_thread(state.speaker.wait_or_interrupt)

            # A reviewable edit offers D: the pending change opens side by
            # side in VS Code. Hint only when it would actually work.
            review = None
            if diff and _code_cli() and review_files(tool_name, tool_input):
                review = (tool_name, tool_input)
            answer_prompt = f"hold {config.PTT_LABEL} to answer (yes / no)"
            if review:
                answer_prompt += f" {TOOL_MARK} d: side-by-side"
            audio = await asyncio.to_thread(
                state.recorder.record_while_held, answer_prompt, review
            )
            if audio is None:
                clear_status()
                print(f"  {dim('no answer — denied')}\n")
                return state.PermissionResultDeny(message=(
                    "No answer was captured from the user — the microphone "
                    "heard nothing, so this is not a refusal. If the call "
                    "still matters, tell the user and request it once more."))

            answer = (await asyncio.to_thread(state.stt.transcribe, audio)).lower()
            clear_status()
            print(f"  {cyan('You »')} {answer.strip()}")
            transcript.append_transcript("You", answer.strip())

            if is_affirmative(answer):
                created = (state.git_safety.before_mutation(tool_name, tool_input)
                           if tool_name in ("Bash", "Edit", "Write") else False)
                # "yes for the whole task" / "yes to all" widens the grant:
                # on an edit, to every remaining edit this task; on a fetch,
                # to THIS DOMAIN's remaining fetches only (an injected
                # redirect to a new domain breaks out of the grant and asks
                # afresh, naming the stranger); on any other tool, to that
                # tool's remaining calls this task.
                scope = None
                if grants_whole_task(answer):
                    if tool_name in ("Edit", "Write"):
                        state._task_grants.add("edits")
                        scope = "edits"
                    elif tool_name == "WebFetch":
                        domain = policy.url_domain(str(tool_input.get("url", "")))
                        if domain:  # no domain, nothing scopeable to grant
                            state._task_grants.add(f"WebFetch:{domain}")
                            scope = f"fetches from {domain}"
                    else:
                        state._task_grants.add(tool_name)
                        scope = f"{tool_name} calls"
                if scope:
                    print(f"  {green('approved')} {dim(f'— and auto-approving {scope} for the rest of this task')}")
                    confirmation = "Okay — I'll handle the rest of those without asking."
                else:
                    print(f"  {green('approved')}")
                    confirmation = "Okay, doing it now."
                transcript.append_transcript("Mabara", confirmation)
                state.speaker.say(confirmation)
                if created:
                    print(f"  {dim(CHECKPOINT_HINT)}")
                print()
                return state.PermissionResultAllow()
            elif is_plain_denial(answer):
                print(f"  {dim('denied')}\n")
                transcript.append_transcript("Mabara", "Okay, I won't do that.")
                state.speaker.say("Okay, I won't do that.")
                return state.PermissionResultDeny(message=(
                    "User declined via voice. Do not retry this tool call — "
                    "if you can't proceed without it, ask the user what "
                    "they'd like instead."))
            else:
                # The answer carried more than a no — a correction, a
                # condition, a redirection. Forward the words instead of
                # discarding them: "no, use port 5433" should steer the
                # next attempt, not dead-end the task.
                spoken = answer.strip()
                print(f"  {dim('denied — feedback passed to Mabara')}\n")
                transcript.append_transcript("Mabara", "Okay — one sec, let me address that.")
                state.speaker.say("Okay — one sec, let me address that.")
                return state.PermissionResultDeny(message=(
                    f'User declined this call and said: "{spoken}". Treat '
                    "that as feedback: revise the plan or the change "
                    "accordingly, and request approval again once it's "
                    "addressed. Don't repeat the identical request."))
    finally:
        state._approvals_pending -= 1
        state._pending_asks[tool_name] -= 1
        start_thinking()
