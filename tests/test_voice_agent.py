"""Unit tests for voice_agent's pure logic — above all the two safety
gates: spoken approval parsing (is_affirmative) and the read-only Bash
allowlist (is_read_only_bash). No audio hardware, models, or SDK needed;
run with: venv/Scripts/python -m pytest tests/
"""

import os
import subprocess
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

# ---------- Diff rendering (shown before every edit approval) ----------

def test_render_diff_edit_marks_changes():
    lines = va.render_diff("Edit", {
        "file_path": "x.py",
        "old_string": "a\nb\nc",
        "new_string": "a\nB\nc",
    })
    assert "-b" in lines and "+B" in lines
    # Snippet diffs drop @@ headers: fragment-relative numbers would lie
    assert not any(line.startswith("@@") for line in lines)


def test_render_diff_identical_content_is_none():
    assert va.render_diff("Edit", {
        "file_path": "x.py", "old_string": "same", "new_string": "same",
    }) is None


def test_render_diff_write_new_file(tmp_path):
    target = tmp_path / "brand_new.txt"
    lines = va.render_diff("Write", {
        "file_path": str(target), "content": "one\ntwo",
    })
    assert "+one" in lines and "+two" in lines
    assert not any(line.startswith("-") and line != "---" for line in lines)


def test_render_diff_write_existing_file_keeps_hunk_headers(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    lines = va.render_diff("Write", {
        "file_path": str(target), "content": "alpha\nBETA\ngamma\n",
    })
    assert "-beta" in lines and "+BETA" in lines
    assert any(line.startswith("@@") for line in lines)


def test_render_diff_other_tools_are_none():
    assert va.render_diff("Bash", {"command": "ls"}) is None


# ---------- Tool outcome markers ----------

class _Block:
    def __init__(self, tool_use_id, is_error=False, content=None):
        self.tool_use_id = tool_use_id
        self.is_error = is_error
        self.content = content


def test_bash_failure_gets_a_marker():
    pending = {"t1": ("Bash", 0.0)}
    line = va.describe_tool_outcome(
        _Block("t1", is_error=True, content="command not found: pyest"), pending)
    assert "bash failed" in line and "pyest" in line
    assert pending == {}  # consumed


def test_read_failures_stay_silent():
    pending = {"t2": ("Read", 0.0)}
    assert va.describe_tool_outcome(
        _Block("t2", is_error=True, content="no such file"), pending) is None


def test_denials_do_not_double_report():
    pending = {"t3": ("Bash", 0.0)}
    assert va.describe_tool_outcome(
        _Block("t3", is_error=True, content="User declined via voice"),
        pending) is None


def test_fast_bash_success_stays_silent():
    import time as _time
    pending = {"t4": ("Bash", _time.time())}
    assert va.describe_tool_outcome(_Block("t4"), pending) is None


def test_slow_bash_success_gets_ok_marker():
    import time as _time
    pending = {"t5": ("Bash", _time.time() - 10)}
    line = va.describe_tool_outcome(_Block("t5"), pending)
    assert "ok" in line and "10s" in line


def test_tool_result_text_handles_block_lists():
    assert va._tool_result_text(
        [{"type": "text", "text": "  \nerror: boom\nmore"}]) == "error: boom"
    assert va._tool_result_text(None) == ""


# ---------- Turn summary formatting ----------

def test_fmt_secs():
    assert va._fmt_secs(42) == "42s"
    assert va._fmt_secs(125) == "2m05s"
    assert va._fmt_secs(59.9) == "59s"


# ---------- GitSafety: mid-session git init + fresh-repo checkpoints ----------

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=30)


def test_recheck_picks_up_mid_session_git_init(tmp_path):
    gs = va.GitSafety(str(tmp_path))
    assert not gs.enabled
    assert not gs.recheck()   # still not a repo: stays blocked
    _git(tmp_path, "init")
    # The deny message promises 'git init enables editing' — recheck is what
    # keeps that promise (live failure 2026-07-05: the cached False pushed
    # the agent into a shell-heredoc workaround).
    assert gs.recheck()
    assert gs.enabled


def test_fresh_repo_checkpoint_reverts_without_head(tmp_path):
    _git(tmp_path, "init")    # no commits yet: HEAD doesn't exist
    existing = tmp_path / "app.js"
    existing.write_text("original", encoding="utf-8")
    created = tmp_path / "new.js"

    gs = va.GitSafety(str(tmp_path))
    assert gs.enabled
    gs.begin_turn("improve the design")
    gs.before_mutation("Edit", {"file_path": str(existing)})
    gs.before_mutation("Write", {"file_path": str(created)})
    existing.write_text("mangled", encoding="utf-8")
    created.write_text("brand new", encoding="utf-8")

    assert not gs._head_at_ckpt   # stash create was skipped, backups taken
    message = gs.revert()
    assert existing.read_text(encoding="utf-8") == "original"
    assert not created.exists()
    assert "restored 1 file" in message and "removed 1 new file" in message


# ---------- Spoken approval questions stay short ----------

def test_spoken_command_short_commands_verbatim():
    assert va.spoken_command("git init") == "the command: git init"


def test_spoken_command_caps_heredocs():
    heredoc = ('cat > "index.html" << EOF\n<!DOCTYPE html>\n'
               + "x\n" * 100 + "EOF")
    text = va.spoken_command(heredoc)
    assert "<!DOCTYPE" not in text
    assert text.startswith('the command: cat > "index.html" << EOF')
    assert "full command is on your screen" in text


def test_spoken_command_caps_long_single_lines():
    text = va.spoken_command("echo " + "a" * 200)
    assert len(text) < 160
    assert "on your screen" in text


def test_describe_action_spoken_variant_truncates_screen_does_not():
    tool_input = {"command": "git status\ngit log --oneline"}
    assert "git log --oneline" in va.describe_action("Bash", tool_input)
    spoken = va.describe_action("Bash", tool_input, spoken=True)
    assert "git log" not in spoken
    assert "on your screen" in spoken


def test_print_command_truncates_like_diffs(capsys):
    va.print_command("\n".join(f"line{i}" for i in range(60)))
    out = capsys.readouterr().out
    assert "line0" in out and "line39" in out
    assert "line45" not in out
    assert "+20 more lines" in out


# ---------- Tool feed honesty ----------

def test_feed_shows_out_of_repo_paths_in_full(repo):
    inside = os.path.join(str(repo), "public", "app.js")
    outside = os.path.join(os.path.dirname(str(repo)), "elsewhere", "app.js")
    assert va.describe_tool_use("Read", {"file_path": inside}) == "read public/app.js"
    # An out-of-repo probe must never be shortened into looking local
    assert outside in va.describe_tool_use("Read", {"file_path": outside})
