"""Unit tests for voice_agent's pure logic — above all the two safety
gates: spoken approval parsing (is_affirmative) and the read-only Bash
allowlist (is_read_only_bash). No audio hardware, models, or SDK needed;
run with: venv/Scripts/python -m pytest tests/
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import voice_agent as va


# ---------- Approval parsing: the gate in front of every edit/command ----------

@pytest.mark.parametrize("answer", [
    "yes",
    "Yes.",
    "yeah",
    "yep",
    "sure",
    "okay",
    "ok",
    "go ahead",
    "sure, go ahead",
    "yes please do it",
])
def test_affirmative_answers_approve(answer):
    assert va.is_affirmative(answer)


@pytest.mark.parametrize("answer", [
    "no",
    "nope",
    "no way",
    "deny",
    "cancel that",
    # Substring traps: 'ok' hides inside these words. A denial that merely
    # contains those letters must never approve.
    "no, let me look at it first",
    "no, that looks broken",
    "no, look into it more",
    # A deny word anywhere vetoes the whole answer
    "yes— wait, actually no",
    "yes, hold on",
    "okay wait",
    "don't",
    "please don't do that",
    # Ambiguous or empty answers fail closed
    "",
    "hmm",
    "what does it change?",
])
def test_non_affirmative_answers_deny(answer):
    assert not va.is_affirmative(answer)


def test_whole_task_grant_needs_the_word_not_the_substring():
    assert va.grants_whole_task("yes for the whole task")
    assert va.grants_whole_task("yes to all")
    assert va.grants_whole_task("yes, everything")
    assert not va.grants_whole_task("yes")
    # 'actually' and 'installed' contain 'all' as a substring only
    assert not va.grants_whole_task("yes, actually go ahead")
    assert not va.grants_whole_task("yes, it should be installed")


# ---------- Read-only Bash allowlist ----------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Point the module's repo confinement at a temp repo root."""
    monkeypatch.setattr(va, "repo_root", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("command", [
    "ls",
    "ls -la",
    "dir",
    "pwd",
    "echo hello",
    "git status",
    "git log -5",
    "git log --oneline",
    "git diff HEAD~1",
])
def test_read_only_commands_are_allowed(repo, command):
    assert va.is_read_only_bash(command)


@pytest.mark.parametrize("command", [
    # Chaining / redirection
    "cat x; rm -rf .",
    "echo hi > file",
    "ls && rm x",
    "cat `whoami`",
    "cat $(cmd)",
    "git status | sh",
    # git branch is a write op (create/force-move/delete), never read-only
    "git branch -D main",
    "git branch new-branch",
    # --output/-o write files with no shell redirection involved
    "git log --output=stolen.txt",
    "git log --output stolen.txt",
    "git diff --output=x",
    "git log -o x",
    # Prefixes must match whole words
    "lsfoo",
    "catalog run",
    "typescript-compile",
    "gitk",
    # Not on the allowlist at all
    "rm -rf /",
    "curl http://evil.example",
    "",
])
def test_unsafe_commands_are_not_auto_approved(repo, command):
    assert not va.is_read_only_bash(command)


def test_cat_confined_to_repo(repo):
    inside = repo / "notes.txt"
    assert va.is_read_only_bash(f"cat {inside}")
    assert va.is_read_only_bash("cat notes.txt")          # relative = in repo
    assert not va.is_read_only_bash("cat ../secret.txt")  # escapes the repo
    assert not va.is_read_only_bash("cat ~/.ssh/id_rsa")
    outside = os.path.join(os.path.dirname(str(repo)), "outside.txt")
    assert not va.is_read_only_bash(f"cat {outside}")


def test_nothing_auto_approves_before_repo_root_is_set(monkeypatch):
    monkeypatch.setattr(va, "repo_root", None)
    assert not va.is_read_only_bash("cat notes.txt")
    assert not va._within_repo("notes.txt")


# ---------- Repo confinement for the Read/Glob/Grep tools ----------

def test_within_repo(repo):
    assert va._within_repo(None)                # no path = tool cwd = repo
    assert va._within_repo("")
    assert va._within_repo(str(repo / "src" / "app.py"))
    assert not va._within_repo(str(repo.parent / "elsewhere.txt"))
    assert not va._within_repo(os.path.expanduser("~/.aws/credentials"))
    # Prefix trickery: /repo-evil must not count as inside /repo
    assert not va._within_repo(str(repo) + "-evil" + os.sep + "f.txt")


# ---------- Voice command matchers ----------

def test_revert_command_matcher():
    assert va.is_revert_command("revert that")
    assert va.is_revert_command("undo your last changes")
    assert not va.is_revert_command("how do I undo a commit?")
    assert not va.is_revert_command("revert the refactor you did yesterday and explain")


def test_commit_command_matcher():
    assert va.is_commit_command("commit this")
    assert va.is_commit_command("commit the changes please")
    assert not va.is_commit_command("what does commit mean?")
    assert not va.is_commit_command("commit fraud")


def test_model_switch_matcher():
    assert va.model_switch_target("switch to sonnet") == "sonnet"
    assert va.model_switch_target("use haiku now") == "haiku"
    assert va.model_switch_target("which model is better, sonnet or haiku?") is None
    assert va.model_switch_target("switch to sonnet or haiku") is None
    assert va.model_switch_target("tell me about opus") is None


# ---------- TTS text cleanup ----------

def test_strip_markdown():
    assert va.strip_markdown("**bold** and *italic* and `code`") == \
        "bold and italic and code"
    assert "##" not in va.strip_markdown("## Heading\ntext")


def test_speakable_shortens_paths():
    assert va.speakable(r"open C:\Users\me\proj\app\page.tsx now") == \
        "open page.tsx now"
    assert va.speakable("see src/components/Button.jsx") == "see Button.jsx"
    assert va.speakable("no paths here") == "no paths here"
