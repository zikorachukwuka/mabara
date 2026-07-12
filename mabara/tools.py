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

from . import commands, config, state, transcript
from .terminal import (
    CHECK, clear_status, cyan, dim, green, start_thinking, status,
    stop_thinking, yellow,
)
from .text import speakable

PLAN_TOOL = "mcp__mabara__propose_plan"
RUN_TESTS_TOOL = "mcp__mabara__run_tests"

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


async def _propose_plan_impl(args):
    """Speak the plan, collect the spoken verdict, convert a yes into the
    task grants. Mirrors voice_permission_callback's concurrency shape:
    the approval lock serializes the mic, the pending counter pauses the
    barge-in watcher so an answer is never read as a cut-off."""
    goal = str(args.get("goal", "")).strip() or "no goal given"
    steps = str(args.get("steps", "")).strip() or "(no steps)"
    files = str(args.get("files", "")).strip() or "(unspecified)"
    verification = str(args.get("verification", "")).strip() or "(none given)"

    state._approvals_pending += 1
    try:
        async with state._approval_lock:
            stop_thinking()
            print(f"\n\n  {yellow('! plan approval')} — Mabara proposes:")
            _print_plan(goal, steps, files, verification)
            spoken_steps = " Then ".join(
                s.strip(" -•") for s in steps.splitlines() if s.strip())
            question = (f"Here's my plan. {goal}. {spoken_steps}. "
                        f"I'll verify by: {verification}. "
                        "It's on your screen too. Do you approve the plan?")
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
                    "and finish by running the verification you promised.")
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
        "verification; shell commands still ask individually.",
        {"goal": str, "steps": str, "files": str, "verification": str},
    )(_propose_plan_impl)

    run_tests = tool(
        "run_tests",
        "Run this repository's test suite. Discovers the test runner "
        "itself (npm test, pytest, cargo test, go test) — do not pass a "
        "command. Returns a summary and the output tail. Use this for "
        "verification instead of a shell command.",
        {},
    )(_run_tests_impl)

    return create_sdk_mcp_server(name="mabara",
                                 tools=[propose_plan, run_tests])
