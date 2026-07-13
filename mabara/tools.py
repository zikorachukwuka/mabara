"""Mabara's in-process tools (SDK MCP server): propose_plan and run_tests.

These are purpose-built replacements for choreography that generic tools
do badly in a voice UX. propose_plan turns "approve twenty diffs one by
one" into "approve the intent once, out loud"; run_tests turns "approve
an arbitrary shell string" into a named, deterministic action whose
command is discovered from the repo — the model never supplies it, so
there is no injection surface.

Tool names as the permission policy sees them:
    mcp__mabara__propose_plan   (allowed always — the tool itself asks)
    mcp__mabara__run_tests      (granted by an approved plan, else asked)
"""

import asyncio
import os
import re
import shutil
import subprocess
import time

from . import commands, config, context, state, transcript
from .terminal import (
    CHECK, clear_status, cyan, dim, green, start_thinking, status,
    stop_thinking, yellow,
)
from .text import speakable

PLAN_TOOL = "mcp__mabara__propose_plan"
RUN_TESTS_TOOL = "mcp__mabara__run_tests"
REPLACE_TOOL = "mcp__mabara__replace_text"
NOTES_TOOL = "mcp__mabara__update_notes"

# The grant an approved plan installs alongside "edits": the plan names
# its verification step, so the yes covers running it.
PLAN_GRANTS = ("edits", RUN_TESTS_TOOL)

TEST_TIMEOUT_SECS = 300
TEST_OUTPUT_TAIL_LINES = 15
PLAN_MAX_PRINT_LINES = 30


# ---------- Test runner discovery (deterministic, never model input) ----------

def _repo_python(repo):
    """The repo's own venv python if it has one — running Mabara's venv
    against someone else's project tests the wrong environment."""
    for venv in ("venv", ".venv"):
        exe = os.path.join(repo, venv, "Scripts", "python.exe")
        if os.path.exists(exe):
            return exe
    return None


def detect_test_command(repo):
    """(argv, label) for this repo's test runner, or None when no runner
    can be found honestly. First match wins; the argv is built from the
    repo's own files and PATH lookups — never from model-supplied text."""
    # npm: a package.json with a real test script
    package_json = os.path.join(repo, "package.json")
    if os.path.exists(package_json):
        try:
            import json
            with open(package_json, encoding="utf-8") as f:
                scripts = json.load(f).get("scripts", {})
            test_script = scripts.get("test", "")
            if test_script and "no test specified" not in test_script:
                npm = shutil.which("npm")
                if npm:
                    return ([npm, "test"], "npm test")
        except (OSError, ValueError):
            pass

    # pytest: config that mentions it, or a tests/ directory
    pytest_hints = False
    for name in ("pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini"):
        path = os.path.join(repo, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if "pytest" in f.read():
                    pytest_hints = True
                    break
        except OSError:
            continue
    if not pytest_hints and os.path.isdir(os.path.join(repo, "tests")):
        pytest_hints = True
    if pytest_hints:
        python = _repo_python(repo)
        if python:
            return ([python, "-m", "pytest"], "pytest")
        pytest_exe = shutil.which("pytest")
        if pytest_exe:
            return ([pytest_exe], "pytest")

    if os.path.exists(os.path.join(repo, "Cargo.toml")):
        cargo = shutil.which("cargo")
        if cargo:
            return ([cargo, "test"], "cargo test")

    if os.path.exists(os.path.join(repo, "go.mod")):
        go = shutil.which("go")
        if go:
            return ([go, "test", "./..."], "go test")

    return None


_PYTEST_SUMMARY = re.compile(
    r"(\d+ (?:passed|failed|error|errors|skipped|xfailed|warnings?)[^\n=]*)")
_JEST_SUMMARY = re.compile(r"Tests?:\s+(.+)")


def summarize_test_output(output, returncode, label):
    """One speakable line for a test run. Parses the shapes pytest and
    jest print; anything else falls back to the exit code."""
    for pattern in (_PYTEST_SUMMARY, _JEST_SUMMARY):
        matches = pattern.findall(output)
        if matches:
            return f"{label}: {matches[-1].strip()}"
    return (f"{label}: all tests passed" if returncode == 0
            else f"{label}: FAILED (exit code {returncode})")


def run_tests_sync(repo):
    """Run the detected test command. Returns the text the model receives —
    a summary line plus the output tail, honest about every failure mode."""
    detected = detect_test_command(repo)
    if detected is None:
        return ("No test runner found in this repo (looked for a package.json "
                "test script, pytest configuration or a tests/ directory, "
                "Cargo.toml, and go.mod). Tell the user, and ask whether "
                "there's a command you should use instead.")
    argv, label = detected
    try:
        proc = subprocess.run(
            argv, cwd=repo, capture_output=True, text=True,
            timeout=TEST_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return (f"{label} timed out after {TEST_TIMEOUT_SECS}s. "
                "Tell the user plainly.")
    except OSError as e:
        return f"Couldn't start {label}: {e}"
    output = (proc.stdout or "") + (proc.stderr or "")
    summary = summarize_test_output(output, proc.returncode, label)
    tail = "\n".join(output.strip().splitlines()[-TEST_OUTPUT_TAIL_LINES:])
    return (f"{summary}\n"
            f"--- last {TEST_OUTPUT_TAIL_LINES} lines ---\n{tail}\n"
            f"(exit code {proc.returncode})")


# ---------- Mass text replacement (the bulk-rename instrument) ----------
# Born from a live failure (2026-07-12): asked to rename a brand string
# across 61 files, the model batch-read them all into context, overflowed
# it, the CLI dropped the read results, and every edit bounced off the
# read-before-edit check. A mechanical same-text replacement must never
# pass through a context window at all — it is one deterministic
# operation with one preview and one spoken yes.

REPLACE_PREVIEW_LINES = 12


def _repo_text_files(repo):
    """Tracked plus untracked-but-not-ignored files, straight from git —
    the honest definition of 'the project', with node_modules, build
    output, and .git excluded by the repo's own ignore rules."""
    gs = state.git_safety
    if gs is None or not gs.enabled:
        return []
    files = []
    for args in (("ls-files",),
                 ("ls-files", "--others", "--exclude-standard")):
        proc = gs._git(*args)
        if proc is not None and proc.returncode == 0:
            files.extend(line for line in proc.stdout.splitlines()
                         if line.strip())
    return files


def scan_replacements(repo, find):
    """[(relpath, count)] of exact-string occurrences, biggest first.
    Files that don't decode as UTF-8 (binaries) are skipped — this tool
    only ever touches text it can rewrite losslessly."""
    hits = []
    for rel in _repo_text_files(repo):
        path = os.path.join(repo, rel)
        try:
            # newline="" both here and in apply: the bytes must round-trip
            # exactly — a rename must never churn CRLF/LF line endings.
            with open(path, "r", encoding="utf-8", newline="") as f:
                count = f.read().count(find)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if count:
            hits.append((rel, count))
    hits.sort(key=lambda item: -item[1])
    return hits


def apply_replacements(repo, hits, find, replace, before_write=None):
    """Rewrite every hit file. before_write(abs_path) runs before each
    write — the hook GitSafety's checkpoint/backup rides in on. Returns
    (files_changed, files_failed)."""
    changed = failed = 0
    for rel, _count in hits:
        path = os.path.join(repo, rel)
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                content = f.read()
            if before_write:
                before_write(os.path.abspath(path))
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content.replace(find, replace))
            changed += 1
        except (OSError, UnicodeDecodeError, ValueError):
            failed += 1
    return changed, failed


async def _replace_text_impl(args):
    """Preview, one spoken yes, replace everywhere under a checkpoint."""
    find = str(args.get("find", ""))
    replace = str(args.get("replace", ""))
    if not find.strip():
        return _tool_text("Empty search string — nothing to replace.")
    if state.git_safety is None or not state.git_safety.enabled:
        return _tool_text(
            "Replacements are disabled: this folder is not a git "
            "repository, so there is no checkpoint to revert to. "
            "Tell the user git init enables it.")

    hits = scan_replacements(state.repo_root, find)
    if not hits:
        return _tool_text(f'No occurrences of "{find}" found in the repo.')
    total = sum(count for _rel, count in hits)

    state._approvals_pending += 1
    try:
        async with state._approval_lock:
            stop_thinking()
            rule = "-" * 46
            print(f"\n\n  {yellow('! replace approval')} — Mabara wants to "
                  f"replace \"{find}\" with \"{replace}\":")
            print(dim("--[ occurrences ]" + rule[17:]))
            for rel, count in hits[:REPLACE_PREVIEW_LINES]:
                print(f"  {rel}  {dim(f'x{count}')}")
            if len(hits) > REPLACE_PREVIEW_LINES:
                print(dim(f"  ... +{len(hits) - REPLACE_PREVIEW_LINES} more files"))
            print(dim(f"  total: {total} occurrences in {len(hits)} files"))
            print(dim(rule))
            question = (f'I found {total} occurrences of "{find}" across '
                        f'{len(hits)} files — the list is on your screen. '
                        f'Replace them all with "{replace}"? You can revert '
                        "afterwards.")
            transcript.append_transcript("Mabara", question)
            state.speaker.say(speakable(question))
            await asyncio.to_thread(state.speaker.wait_or_interrupt)

            audio = await asyncio.to_thread(
                state.recorder.record_while_held,
                f"hold {config.PTT_LABEL} to answer (yes / no)")
            if audio is None:
                clear_status()
                print(f"  {dim('no answer — not replacing')}\n")
                return _tool_text(
                    "No answer was captured — the microphone heard nothing, "
                    "so this is not a refusal. Ask the user to repeat.")
            answer = (await asyncio.to_thread(
                state.stt.transcribe, audio)).lower()
            clear_status()
            print(f"  {cyan('You »')} {answer.strip()}")
            transcript.append_transcript("You", answer.strip())

            if commands.is_affirmative(answer):
                def before_write(path):
                    state.git_safety.before_mutation(
                        "Edit", {"file_path": path})
                changed, failed = await asyncio.to_thread(
                    apply_replacements, state.repo_root, hits, find,
                    replace, before_write)
                print(f"  {green('replaced')} {dim(f'— {total} occurrences in {changed} files')}")
                if failed:
                    print(f"  {dim(f'({failed} files could not be rewritten)')}")
                print(f"  {dim(f'{CHECK} checkpoint saved — say revert that to undo')}\n")
                outcome = (f"Done — replaced {total} occurrences of "
                           f'"{find}" with "{replace}" in {changed} files.')
                if failed:
                    outcome += f" {failed} files failed and were left alone."
                state.speaker.say(speakable(outcome))
                transcript.append_transcript("Mabara", outcome)
                return _tool_text(
                    outcome + " A checkpoint was taken; 'revert that' "
                    "undoes it. Verify with a grep if it matters — note "
                    "this tool counts occurrences while grep counts "
                    "matching lines, so totals can differ when a line "
                    "contains the text twice. The user already HEARD this "
                    "result spoken: don't repeat the numbers, just "
                    "continue.")
            elif commands.is_plain_denial(answer):
                print(f"  {dim('not replacing')}\n")
                state.speaker.say("Okay, leaving everything as it is.")
                return _tool_text("User declined the replacement. Nothing "
                                  "was changed. Ask what they'd like.")
            else:
                print(f"  {dim('feedback — passed to Mabara')}\n")
                state.speaker.say("Okay — let me adjust.")
                return _tool_text(
                    f'User answered: "{answer.strip()}". Nothing was '
                    "changed. Treat that as feedback and adjust — perhaps "
                    "a different search or replacement string.")
    finally:
        state._approvals_pending -= 1
        start_thinking()


# ---------- Session notes (the agent's own per-repo notebook) ----------

async def _update_notes_impl(args):
    """Replace this repo's session notes. Confined by construction: the
    only path it can write is the notes file for the CURRENT repo, which
    lives in Mabara's data dir — never inside any repo, never CLAUDE.md
    (the human's instruction file stays the human's)."""
    text = str(args.get("notes", ""))
    if not text.strip():
        return _tool_text("Empty notes — nothing saved. Pass the complete "
                          "new notes text.")
    clipped = await asyncio.to_thread(
        context.save_repo_notes, state.repo_root, text)
    clear_status()
    lines = len(text.strip().splitlines())
    print(f"  {dim(f'{CHECK} session notes updated ({lines} lines)')}")
    if clipped:
        return _tool_text(
            "Notes saved but CLIPPED at the size cap — tighten them: keep "
            "only durable, verified facts and drop anything stale.")
    return _tool_text("Notes saved. They'll be in your context next "
                      "session on this repo.")


# ---------- The spoken plan approval ----------

def _print_plan(goal, steps, files, verification):
    rule = "-" * 46
    clear_status()
    print(dim("--[ plan ]" + rule[10:]))
    lines = [f"goal: {goal}", "steps:"]
    lines += [f"  {line}" for line in str(steps).strip().splitlines()]
    lines += [f"files: {files}", f"verify: {verification}"]
    for line in lines[:PLAN_MAX_PRINT_LINES]:
        print(f"  {line}")
    if len(lines) > PLAN_MAX_PRINT_LINES:
        print(dim(f"  ... +{len(lines) - PLAN_MAX_PRINT_LINES} more lines"))
    print(dim(rule))


def _tool_text(text):
    return {"content": [{"type": "text", "text": text}]}


def plan_spoken_question(goal, steps, verification, revision_note=""):
    """What the plan approval says out loud: the CONTRACT — goal, step
    count, verification — never the step-by-step, which is on screen
    (same split as diffs: 'the diff is on your screen'). The listener can
    ask to hear the steps (see wants_readout); a RE-proposal after
    feedback speaks only what changed. Reading full plans aloud fatigued
    a live user into non-approvals by the third revision."""
    if str(revision_note).strip():
        return (f"Updated plan — {str(revision_note).strip()}. "
                "The full plan is on your screen. Do you approve the plan?")
    n = len([s for s in str(steps).splitlines() if s.strip()])
    count = "one step" if n == 1 else f"{n} steps"
    return (f"Here's the plan — {count}. {goal}. "
            f"I'll verify by: {verification}. The full steps are on your "
            "screen. Approve the plan, or say read it out to hear the steps.")


def plan_steps_speech(steps):
    """The on-request read-out — the eyes-free path stays available."""
    spoken = " Then ".join(
        s.strip(" -•") for s in str(steps).splitlines() if s.strip())
    return f"The steps: {spoken}. Do you approve the plan?"


_READOUT_WORDS = {"read", "hear"}


def wants_readout(answer):
    """'read it out' / 'let me hear the steps' — checked before the
    yes/no/feedback branches; a pure yes never contains these words."""
    words = set(re.findall(r"[a-z']+", str(answer).lower()))
    return bool(words & _READOUT_WORDS)


async def _propose_plan_impl(args):
    """Speak the plan, collect the spoken verdict, convert a yes into the
    task grants. Mirrors voice_permission_callback's concurrency shape:
    the approval lock serializes the mic, the pending counter pauses the
    barge-in watcher so an answer is never read as a cut-off."""
    goal = str(args.get("goal", "")).strip() or "no goal given"
    steps = str(args.get("steps", "")).strip() or "(no steps)"
    files = str(args.get("files", "")).strip() or "(unspecified)"
    verification = str(args.get("verification", "")).strip() or "(none given)"
    revision_note = str(args.get("revision_note", "")).strip()

    state._approvals_pending += 1
    try:
        async with state._approval_lock:
            stop_thinking()
            banner = ("! plan approval (revised)" if revision_note
                      else "! plan approval")
            print(f"\n\n  {yellow(banner)} — Mabara proposes:")
            _print_plan(goal, steps, files, verification)
            question = plan_spoken_question(goal, steps, verification,
                                            revision_note)
            spoken_question = speakable(question)
            transcript.append_transcript("Mabara", spoken_question)
            state.speaker.say(spoken_question)
            await asyncio.to_thread(state.speaker.wait_or_interrupt)

            audio = await asyncio.to_thread(
                state.recorder.record_while_held,
                f"hold {config.PTT_LABEL} to answer the plan (yes / no)")
            if audio is None:
                clear_status()
                print(f"  {dim('no answer — plan not approved')}\n")
                return _tool_text(
                    "No answer was captured — the microphone heard nothing, "
                    "so this is not a refusal. Ask the user to repeat.")
            answer = (await asyncio.to_thread(
                state.stt.transcribe, audio)).lower()
            clear_status()
            print(f"  {cyan('You »')} {answer.strip()}")
            transcript.append_transcript("You", answer.strip())

            # "Read it out": the eyes-free path — speak the steps once,
            # then take the real answer.
            if wants_readout(answer):
                readout = plan_steps_speech(steps)
                transcript.append_transcript("Mabara", readout)
                state.speaker.say(speakable(readout))
                await asyncio.to_thread(state.speaker.wait_or_interrupt)
                audio = await asyncio.to_thread(
                    state.recorder.record_while_held,
                    f"hold {config.PTT_LABEL} to answer the plan (yes / no)")
                if audio is None:
                    clear_status()
                    print(f"  {dim('no answer — plan not approved')}\n")
                    return _tool_text(
                        "No answer was captured after the read-out — not a "
                        "refusal. Ask the user to repeat.")
                answer = (await asyncio.to_thread(
                    state.stt.transcribe, audio)).lower()
                clear_status()
                print(f"  {cyan('You »')} {answer.strip()}")
                transcript.append_transcript("You", answer.strip())

            if commands.is_affirmative(answer):
                for grant in PLAN_GRANTS:
                    state._task_grants.add(grant)
                print(f"  {green('plan approved')} {dim('— edits and the test run are pre-approved for this task')}")
                print(f"  {dim(CHECK + ' every change still prints its diff, and gets a checkpoint')}\n")
                confirmation = "Plan approved — I'll get to work."
                transcript.append_transcript("Mabara", confirmation)
                state.speaker.say(confirmation)
                return _tool_text(
                    "Plan APPROVED by voice. Edits inside this repo and the "
                    "run_tests tool are pre-approved for the rest of this "
                    "task; shell commands still ask individually. Execute "
                    "the plan step by step, narrating significant steps, "
                    "and finish by running the verification you promised. "
                    "The user already heard the approval confirmed — don't "
                    "re-announce it, start working.")
            elif commands.is_plain_denial(answer):
                print(f"  {dim('plan declined')}\n")
                state.speaker.say("Okay, I'll hold off.")
                return _tool_text(
                    "User declined the plan. Don't start the work. Ask what "
                    "they'd like changed, or wait for direction.")
            else:
                print(f"  {dim('plan feedback — passed to Mabara')}\n")
                state.speaker.say("Got it — let me rework the plan.")
                return _tool_text(
                    f'User answered: "{answer.strip()}". Treat that as '
                    "feedback on the plan: revise it and propose again. "
                    "Nothing is approved yet.")
    finally:
        state._approvals_pending -= 1
        start_thinking()


async def _run_tests_impl(_args):
    """Run the repo's tests off-thread. _tool_busy keeps the stall watcher
    from calling a two-minute pytest run a hang. If the user barges in
    mid-run, the turn dies but the subprocess runs to its timeout — a
    bounded, accepted leak."""
    state._tool_busy += 1
    started = time.time()
    status(dim("running tests..."))
    try:
        result = await asyncio.to_thread(run_tests_sync, state.repo_root)
    finally:
        state._tool_busy -= 1
    clear_status()
    took = time.time() - started
    first_line = result.splitlines()[0] if result else "no output"
    print(f"  {dim(f'{CHECK} tests: {first_line} · {took:.0f}s')}")
    return _tool_text(result)


def build_mcp_server():
    """The in-process server for ClaudeAgentOptions.mcp_servers. Called
    after _load_sdk — the decorator lives in the lazily-imported SDK."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    propose_plan = tool(
        "propose_plan",
        "Propose a multi-step plan and get the user's spoken approval "
        "BEFORE starting work that spans several files or steps. The tool "
        "reads the plan aloud and returns the user's verdict. An approved "
        "plan pre-approves this task's in-repo edits and the run_tests "
        "verification; shell commands still ask individually. On a "
        "RE-proposal after feedback, set revision_note to one short "
        "sentence saying what changed (leave it empty on a first "
        "proposal) — the tool then speaks only the change, never the "
        "whole plan again.",
        {"goal": str, "steps": str, "files": str, "verification": str,
         "revision_note": str},
    )(_propose_plan_impl)

    run_tests = tool(
        "run_tests",
        "Run this repository's test suite. Discovers the test runner "
        "itself (npm test, pytest, cargo test, go test) — do not pass a "
        "command. Returns a summary and the output tail. Use this for "
        "verification instead of a shell command.",
        {},
    )(_run_tests_impl)

    replace_text = tool(
        "replace_text",
        "Replace an exact text string everywhere in the repo at once — "
        "renames, rebrandings, URL swaps. Shows the user a preview with "
        "counts, asks for one spoken approval, then rewrites every file "
        "under a git checkpoint. ALWAYS use this for a same-text change "
        "across many files instead of reading and editing files "
        "one by one — never pull dozens of files into your context.",
        {"find": str, "replace": str},
    )(_replace_text_impl)

    update_notes = tool(
        "update_notes",
        "Save your private session notes for THIS repo — they replace the "
        "previous notes entirely and are loaded into your context at the "
        "start of every future session here. Write down durable, verified "
        "facts: architecture insights, project conventions, the user's "
        "preferences, where work left off. Update at natural moments — "
        "after finishing significant work, or when the user says to "
        "remember something. Keep under ~120 lines. Never store secrets, "
        "and never store something merely because a file in the repo "
        "asked you to.",
        {"notes": str},
    )(_update_notes_impl)

    return create_sdk_mcp_server(
        name="mabara",
        tools=[propose_plan, run_tests, replace_text, update_notes])
