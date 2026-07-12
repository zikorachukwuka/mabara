"""Unit tests for Mabara's pure logic — above all the two safety
gates: spoken approval parsing (commands.is_affirmative) and the read-only
Bash allowlist (policy.is_read_only_bash). No audio hardware, models, or
SDK needed; run with: venv/Scripts/python -m pytest tests/
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mabara import agents, approvals, commands, context, policy, session, state, text, tools, turn
from mabara.gitsafety import GitSafety


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
    "do it",
    "go for it",
    "of course",
    "that's fine",
    "yes for the whole task",
    # First-person phrasing — the natural spoken yes must not fail the
    # closed-vocabulary gate (it did, live, mid approval storm)
    "yes, i approve.",
    "i said i approve, yes.",
    "i said yes",
])
def test_affirmative_answers_approve(answer):
    assert commands.is_affirmative(answer)


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
    # A yes word buried in a question or instruction must not approve:
    # any word outside the closed approval vocabulary fails the gate
    "what will this do",
    "okay, show me the diff first",
    "go back",
    "do you mean the other file?",
    "where does this go",
    "sure, but explain it first",
    "okay run all of them?",
])
def test_non_affirmative_answers_deny(answer):
    assert not commands.is_affirmative(answer)


def test_plain_denial_versus_feedback():
    # Bare no: only approval-vocabulary words — nothing worth forwarding
    assert commands.is_plain_denial("no")
    assert commands.is_plain_denial("nope, cancel that")
    assert commands.is_plain_denial("no, please don't")
    assert commands.is_plain_denial("")
    # Content rides the denial — forwarded to the model, not discarded
    assert not commands.is_plain_denial("no, use port five instead")
    assert not commands.is_plain_denial("yes but rename the table first")
    assert not commands.is_plain_denial("hold on, what does this change?")


def test_whole_task_grant_needs_the_word_not_the_substring():
    assert commands.grants_whole_task("yes for the whole task")
    assert commands.grants_whole_task("yes to all")
    assert commands.grants_whole_task("yes, everything")
    assert not commands.grants_whole_task("yes")
    # 'actually' and 'installed' contain 'all' as a substring only
    assert not commands.grants_whole_task("yes, actually go ahead")
    assert not commands.grants_whole_task("yes, it should be installed")


# ---------- Read-only Bash allowlist ----------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Point repo confinement (state.repo_root) at a temp repo root."""
    monkeypatch.setattr(state, "repo_root", str(tmp_path))
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
    assert policy.is_read_only_bash(command)


@pytest.mark.parametrize("command", [
    # Chaining / redirection
    "cat x; rm -rf .",
    "echo hi > file",
    "ls && rm x",
    "cat `whoami`",
    "cat $(cmd)",
    # $ expansion can leak env vars into the transcript
    "echo $AWS_SECRET_ACCESS_KEY",
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
    assert not policy.is_read_only_bash(command)


def test_cat_confined_to_repo(repo):
    inside = repo / "notes.txt"
    assert policy.is_read_only_bash(f"cat {inside}")
    assert policy.is_read_only_bash("cat notes.txt")          # relative = in repo
    assert not policy.is_read_only_bash("cat ../secret.txt")  # escapes the repo
    assert not policy.is_read_only_bash("cat ~/.ssh/id_rsa")
    outside = os.path.join(os.path.dirname(str(repo)), "outside.txt")
    assert not policy.is_read_only_bash(f"cat {outside}")


def test_nothing_auto_approves_before_repo_root_is_set(monkeypatch):
    monkeypatch.setattr(state, "repo_root", None)
    assert not policy.is_read_only_bash("cat notes.txt")
    assert not policy._within_repo("notes.txt")


# ---------- Repo confinement for the Read/Glob/Grep tools ----------

def test_within_repo(repo):
    assert policy._within_repo(None)                # no path = tool cwd = repo
    assert policy._within_repo("")
    assert policy._within_repo(str(repo / "src" / "app.py"))
    assert not policy._within_repo(str(repo.parent / "elsewhere.txt"))
    assert not policy._within_repo(os.path.expanduser("~/.aws/credentials"))
    # Prefix trickery: /repo-evil must not count as inside /repo
    assert not policy._within_repo(str(repo) + "-evil" + os.sep + "f.txt")


def test_symlink_inside_repo_cannot_point_out(repo):
    # A link committed in an untrusted repo aimed at the home directory
    # must not carry repo confinement with it (realpath, not abspath).
    link = repo / "innocent"
    try:
        os.symlink(os.path.expanduser("~"), str(link))
    except OSError:
        pytest.skip("symlink creation not permitted on this setup")
    assert not policy._within_repo(str(link / ".aws" / "credentials"))


# ---------- Permission policy core (permission_decision) ----------

def _decide(tool, tool_input, readonly=False, task_grants=frozenset(),
            git_enabled=True, web_allowlist=frozenset()):
    return policy.permission_decision(tool, tool_input, readonly=readonly,
                                      task_grants=task_grants,
                                      git_enabled=git_enabled,
                                      web_allowlist=web_allowlist)


def test_reads_inside_repo_auto_approve(repo):
    assert _decide("Read", {"file_path": str(repo / "a.py")}) == ("allow", "read")
    assert _decide("Grep", {"pattern": "x", "path": str(repo)}) == ("allow", "read")
    assert _decide("Glob", {"pattern": "**/*.py"}) == ("allow", "read")


def test_reads_outside_repo_fall_to_voice_ask(repo):
    assert _decide("Read", {
        "file_path": os.path.expanduser("~/.aws/credentials")}) == ("ask", None)
    assert _decide("Grep", {"pattern": "key", "path": str(repo.parent)}) == ("ask", None)


def test_glob_absolute_pattern_cannot_escape_the_repo(repo):
    # An absolute pattern with no path key used to bypass confinement
    # entirely (_within_repo(None) is True) — filename reconnaissance
    assert _decide("Glob", {"pattern": str(repo.parent / "**" / "*")}) == ("ask", None)
    assert _decide("Glob", {"pattern": "~/.ssh/*"}) == ("ask", None)
    assert _decide("Glob", {
        "pattern": str(repo / "src" / "**" / "*.py")}) == ("allow", "read")


def test_readonly_denies_mutating_tools_even_allowlisted_bash(repo):
    for tool, tool_input in [("Edit", {"file_path": str(repo / "a.py")}),
                             ("Write", {"file_path": str(repo / "a.py")}),
                             ("Bash", {"command": "ls"})]:
        assert _decide(tool, tool_input, readonly=True) == ("deny", policy.READONLY_DENY)


def test_bash_allowlist_allows_and_everything_else_asks(repo):
    assert _decide("Bash", {"command": "git status"}) == ("allow", "bash")
    assert _decide("Bash", {"command": "rm -rf ."}) == ("ask", None)


def test_edits_denied_without_git(repo):
    assert _decide("Edit", {"file_path": str(repo / "a.py")},
                   git_enabled=False) == ("deny", policy.NO_GIT_DENY)


def test_whole_task_grant_is_repo_confined(repo):
    inside = {"file_path": str(repo / "src" / "app.py")}
    outside = {"file_path": os.path.expanduser("~/.bashrc")}
    grants = {"edits"}
    assert _decide("Edit", inside, task_grants=grants) == ("allow", "task-grant")
    # The grant must not widen into a license to write outside the repo:
    # an out-of-repo target goes back to a voice ask
    assert _decide("Edit", outside, task_grants=grants) == ("ask", None)
    assert _decide("Write", outside, task_grants=grants) == ("ask", None)
    # Without the grant, even in-repo edits ask
    assert _decide("Edit", inside) == ("ask", None)


def test_bash_never_rides_the_task_grant(repo):
    # Neither the edits grant nor even its own name lets Bash through
    assert _decide("Bash", {"command": "rm -rf ."},
                   task_grants={"edits"}) == ("ask", None)
    assert _decide("Bash", {"command": "rm -rf ."},
                   task_grants={"Bash"}) == ("ask", None)


def test_tool_grant_covers_only_that_tool(repo):
    # "Yes to all" on a web search covers the task's remaining searches...
    assert _decide("WebSearch", {"query": "x"},
                   task_grants={"WebSearch"}) == ("allow", "task-grant")
    # ...but not other tools, and never edits
    assert _decide("WebFetch", {"url": "https://x"},
                   task_grants={"WebSearch"}) == ("ask", None)
    assert _decide("Edit", {"file_path": str(repo / "a.py")},
                   task_grants={"WebSearch", "Edit"}) == ("ask", None)
    # Without a grant, searches still ask every time
    assert _decide("WebSearch", {"query": "x"}) == ("ask", None)


def test_unknown_tools_always_ask(repo):
    assert _decide("NotebookEdit", {
        "notebook_path": str(repo / "n.ipynb")}) == ("ask", None)


def test_out_of_repo_edit_is_flagged_in_the_approval_question(repo):
    inside = approvals.describe_action("Edit", {"file_path": str(repo / "a.py")})
    assert "outside this repo" not in inside
    outside = approvals.describe_action("Write", {
        "file_path": os.path.expanduser("~/.bashrc")})
    assert "outside this repo" in outside


# ---------- Voice command matchers ----------

def test_revert_command_matcher():
    assert commands.is_revert_command("revert that")
    assert commands.is_revert_command("undo your last changes")
    assert not commands.is_revert_command("how do I undo a commit?")
    assert not commands.is_revert_command("revert the refactor you did yesterday and explain")


def test_commit_command_matcher():
    assert commands.is_commit_command("commit this")
    assert commands.is_commit_command("commit the changes please")
    assert not commands.is_commit_command("what does commit mean?")
    assert not commands.is_commit_command("commit fraud")


def test_model_switch_matcher():
    assert commands.model_switch_target("switch to sonnet") == "sonnet"
    assert commands.model_switch_target("use haiku now") == "haiku"
    assert commands.model_switch_target("which model is better, sonnet or haiku?") is None
    assert commands.model_switch_target("switch to sonnet or haiku") is None
    assert commands.model_switch_target("tell me about opus") is None


def test_normalize_model_arg():
    # Only spelling of the bare alias is fixed up.
    assert commands.normalize_model_arg("sonnet") == "sonnet"
    assert commands.normalize_model_arg("Sonnet") == "sonnet"
    assert commands.normalize_model_arg("sonet") == "sonnet"
    assert commands.normalize_model_arg("opus") == "opus"
    assert commands.normalize_model_arg("claude-sonnet-5") == "claude-sonnet-5"
    assert commands.normalize_model_arg("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    # A version number tacked onto the alias is ambiguous (which version?)
    # and is deliberately left untouched — caught later by the startup
    # validation in main(), not silently guessed at here.
    assert commands.normalize_model_arg("sonnet5") == "sonnet5"
    assert commands.normalize_model_arg("sonnet4.6") == "sonnet4.6"
    assert commands.normalize_model_arg("opus3") == "opus3"


# ---------- TTS text cleanup ----------

def test_strip_markdown():
    assert text.strip_markdown("**bold** and *italic* and `code`") == \
        "bold and italic and code"
    assert "##" not in text.strip_markdown("## Heading\ntext")


def test_strip_markdown_links_speak_text_not_url():
    assert text.strip_markdown("see [the docs](https://example.com/a/b) here") == \
        "see the docs here"
    assert text.strip_markdown("![diagram](img/arch.png)") == "diagram"


def test_speakable_shortens_paths():
    assert text.speakable(r"open C:\Users\me\proj\app\page.tsx now") == \
        "open page.tsx now"
    assert text.speakable("see src/components/Button.jsx") == "see Button.jsx"
    assert text.speakable("no paths here") == "no paths here"

# ---------- Diff rendering (shown before every edit approval) ----------

def test_render_diff_edit_marks_changes():
    lines = approvals.render_diff("Edit", {
        "file_path": "x.py",
        "old_string": "a\nb\nc",
        "new_string": "a\nB\nc",
    })
    assert "-b" in lines and "+B" in lines
    # Snippet diffs drop @@ headers: fragment-relative numbers would lie
    assert not any(line.startswith("@@") for line in lines)


def test_render_diff_identical_content_is_none():
    assert approvals.render_diff("Edit", {
        "file_path": "x.py", "old_string": "same", "new_string": "same",
    }) is None


def test_render_diff_write_new_file(tmp_path):
    target = tmp_path / "brand_new.txt"
    lines = approvals.render_diff("Write", {
        "file_path": str(target), "content": "one\ntwo",
    })
    assert "+one" in lines and "+two" in lines
    assert not any(line.startswith("-") and line != "---" for line in lines)


def test_render_diff_write_existing_file_keeps_hunk_headers(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    lines = approvals.render_diff("Write", {
        "file_path": str(target), "content": "alpha\nBETA\ngamma\n",
    })
    assert "-beta" in lines and "+BETA" in lines
    assert any(line.startswith("@@") for line in lines)


def test_render_diff_other_tools_are_none():
    assert approvals.render_diff("Bash", {"command": "ls"}) is None


# ---------- Side-by-side review (press D during an edit approval) ----------

def test_review_files_edit_reconstructs_whole_file(tmp_path):
    p = tmp_path / "app.py"
    p.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    current, proposed, name = approvals.review_files("Edit", {
        "file_path": str(p), "old_string": "b = 2", "new_string": "b = 20"})
    assert current == "a = 1\nb = 2\nc = 3\n"
    assert proposed == "a = 1\nb = 20\nc = 3\n"
    assert name == "app.py"


def test_review_files_edit_replace_all(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("y y y", encoding="utf-8")
    _, proposed, _ = approvals.review_files("Edit", {
        "file_path": str(p), "old_string": "y", "new_string": "z",
        "replace_all": True})
    assert proposed == "z z z"


def test_review_files_write_new_file(tmp_path):
    p = tmp_path / "new.md"
    current, proposed, name = approvals.review_files("Write", {
        "file_path": str(p), "content": "# hi\n"})
    assert current == "" and proposed == "# hi\n" and name == "new.md"


def test_review_files_unreconstructable_is_none(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    # old_string not in the file: the outcome can't be shown honestly
    assert approvals.review_files("Edit", {
        "file_path": str(p), "old_string": "absent",
        "new_string": "x"}) is None
    # no-op Write and non-edit tools have nothing to review
    assert approvals.review_files("Write", {
        "file_path": str(p), "content": "hello"}) is None
    assert approvals.review_files("Bash", {"command": "ls"}) is None
    assert approvals.review_files("Edit", {"old_string": "a",
                                           "new_string": "b"}) is None


def test_open_review_writes_both_sides_and_launches(tmp_path, monkeypatch):
    src = tmp_path / "page.tsx"
    src.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(approvals, "REVIEW_DIR", str(tmp_path / "review"))
    monkeypatch.setattr(approvals, "_code_cli_cache", r"C:\fake\code.cmd")
    launches = []
    monkeypatch.setattr(approvals.subprocess, "Popen",
                        lambda args, **kw: launches.append(args))
    assert approvals.open_review("Write", {"file_path": str(src),
                                           "content": "new\n"})
    (args,) = launches
    assert args[0] == r"C:\fake\code.cmd" and args[1] == "--diff"
    with open(args[2], encoding="utf-8") as f:
        assert f.read() == "old\n"       # left: the file as it is
    with open(args[3], encoding="utf-8") as f:
        assert f.read() == "new\n"       # right: the pending change
    assert args[2].endswith("current-page.tsx")  # extension kept for colors


def test_open_review_without_code_cli_is_false(tmp_path, monkeypatch):
    src = tmp_path / "a.py"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(approvals, "_code_cli_cache", None)
    assert not approvals.open_review("Write", {"file_path": str(src),
                                               "content": "y"})


# ---------- Tool outcome markers ----------

class _Block:
    def __init__(self, tool_use_id, is_error=False, content=None):
        self.tool_use_id = tool_use_id
        self.is_error = is_error
        self.content = content


def test_bash_failure_gets_a_marker():
    pending = {"t1": ("Bash", 0.0)}
    line = approvals.describe_tool_outcome(
        _Block("t1", is_error=True, content="command not found: pyest"), pending)
    assert "bash failed" in line and "pyest" in line
    assert pending == {}  # consumed


def test_read_failures_stay_silent():
    pending = {"t2": ("Read", 0.0)}
    assert approvals.describe_tool_outcome(
        _Block("t2", is_error=True, content="no such file"), pending) is None


def test_denials_do_not_double_report():
    for message in [
        "User declined via voice. Do not retry this tool call — if you "
        "can't proceed without it, ask the user what they'd like instead.",
        "No answer was captured from the user — the microphone heard "
        "nothing, so this is not a refusal.",
        'User declined this call and said: "no, use port five instead". '
        "Treat that as feedback: revise the plan or the change accordingly.",
    ]:
        assert approvals.describe_tool_outcome(
            _Block("t3", is_error=True, content=message),
            {"t3": ("Bash", 0.0)}) is None


def test_fast_bash_success_stays_silent():
    import time as _time
    pending = {"t4": ("Bash", _time.time())}
    assert approvals.describe_tool_outcome(_Block("t4"), pending) is None


def test_slow_bash_success_gets_ok_marker():
    import time as _time
    pending = {"t5": ("Bash", _time.time() - 10)}
    line = approvals.describe_tool_outcome(_Block("t5"), pending)
    assert "ok" in line and "10s" in line


def test_tool_result_text_handles_block_lists():
    assert approvals._tool_result_text(
        [{"type": "text", "text": "  \nerror: boom\nmore"}]) == "error: boom"
    assert approvals._tool_result_text(None) == ""


# ---------- Turn summary formatting ----------

def test_fmt_secs():
    assert turn._fmt_secs(42) == "42s"
    assert turn._fmt_secs(125) == "2m05s"
    assert turn._fmt_secs(59.9) == "59s"


# ---------- GitSafety: mid-session git init + fresh-repo checkpoints ----------

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=30)


def test_recheck_picks_up_mid_session_git_init(tmp_path):
    gs = GitSafety(str(tmp_path))
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

    gs = GitSafety(str(tmp_path))
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
    assert approvals.spoken_command("git init") == "the command: git init"


def test_spoken_command_caps_heredocs():
    heredoc = ('cat > "index.html" << EOF\n<!DOCTYPE html>\n'
               + "x\n" * 100 + "EOF")
    spoken = approvals.spoken_command(heredoc)
    assert "<!DOCTYPE" not in spoken
    assert spoken.startswith('the command: cat > "index.html" << EOF')
    assert "full command is on your screen" in spoken


def test_spoken_command_caps_long_single_lines():
    spoken = approvals.spoken_command("echo " + "a" * 200)
    assert len(spoken) < 160
    assert "on your screen" in spoken


def test_describe_action_spoken_variant_truncates_screen_does_not():
    tool_input = {"command": "git status\ngit log --oneline"}
    assert "git log --oneline" in approvals.describe_action("Bash", tool_input)
    spoken = approvals.describe_action("Bash", tool_input, spoken=True)
    assert "git log" not in spoken
    assert "on your screen" in spoken


def test_print_command_truncates_like_diffs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(approvals, "COMMAND_SPILL_FILE", str(tmp_path / "cmd.txt"))
    approvals.print_command("\n".join(f"line{i}" for i in range(60)))
    out = capsys.readouterr().out
    assert "line0" in out and "line39" in out
    assert "line45" not in out
    assert "+20 more lines" in out


# ---------- Truncated previews spill their full text ----------

def test_truncated_diff_spills_full_text_and_points_to_it(
        tmp_path, monkeypatch, capsys):
    spill = tmp_path / "last.diff"
    monkeypatch.setattr(approvals, "DIFF_SPILL_FILE", str(spill))
    lines = [f"+line{i}" for i in range(60)]
    approvals.print_diff(lines, "app/page.tsx")
    out = capsys.readouterr().out
    assert "+20 more lines" in out
    assert str(spill) in out            # the marker carries the path
    assert spill.read_text(encoding="utf-8").splitlines() == lines


def test_short_diff_spills_nothing(tmp_path, monkeypatch, capsys):
    spill = tmp_path / "last.diff"
    monkeypatch.setattr(approvals, "DIFF_SPILL_FILE", str(spill))
    approvals.print_diff(["+one", "-two"], "app/page.tsx")
    out = capsys.readouterr().out
    assert "more lines" not in out
    assert not spill.exists()


def test_truncated_command_spills_verbatim(tmp_path, monkeypatch, capsys):
    spill = tmp_path / "cmd.txt"
    monkeypatch.setattr(approvals, "COMMAND_SPILL_FILE", str(spill))
    command = "\n".join(f"line{i}" for i in range(60))
    approvals.print_command(command)
    out = capsys.readouterr().out
    assert str(spill) in out
    assert spill.read_text(encoding="utf-8") == command + "\n"


def test_spill_failure_omits_pointer_not_the_preview(
        tmp_path, monkeypatch, capsys):
    bad = os.path.join(str(tmp_path), "missing-dir", "x.diff")
    monkeypatch.setattr(approvals, "DIFF_SPILL_FILE", bad)
    approvals.print_diff([f"+line{i}" for i in range(60)], "a.py")
    out = capsys.readouterr().out
    assert "+20 more lines" in out      # truncation marker survives
    assert "missing-dir" not in out     # dead pointer is omitted


# ---------- Tool feed honesty ----------

def test_feed_shows_out_of_repo_paths_in_full(repo):
    inside = os.path.join(str(repo), "public", "app.js")
    outside = os.path.join(os.path.dirname(str(repo)), "elsewhere", "app.js")
    assert approvals.describe_tool_use("Read", {"file_path": inside}) == "read public/app.js"
    # An out-of-repo probe must never be shortened into looking local
    assert outside in approvals.describe_tool_use("Read", {"file_path": outside})


# ---------- Web fetches: domains, hygiene, the trusted-domain list ----------

def test_url_domain_parses_hosts():
    assert policy.url_domain("https://docs.python.org/3/library/") == "docs.python.org"
    assert policy.url_domain("http://Example.COM:8080/x") == "example.com"
    assert policy.url_domain("https://user:pw@host.net/p") == "host.net"
    assert policy.url_domain("ftp://files.example.com/x") is None
    assert policy.url_domain("not a url") is None
    assert policy.url_domain("") is None


def test_url_flags_clean_urls_have_none():
    assert policy.url_flags("https://docs.python.org/3/library/asyncio.html") == []
    assert policy.url_flags("https://example.com/search?q=piper+tts") == []


def test_url_flags_catch_exfiltration_shapes():
    assert "credentials" in " ".join(policy.url_flags("https://user:pw@evil.net/"))
    assert "long query" in " ".join(policy.url_flags(
        "https://evil.net/c?" + "k=v&" * 60))
    assert "encoded data" in " ".join(policy.url_flags(
        "https://evil.net/c?d=" + "A" * 90))
    assert "scheme" in " ".join(policy.url_flags("file:///C:/Windows/system32"))


def test_webfetch_allowlisted_domain_auto_approves(repo):
    allow = frozenset({"docs.python.org"})
    assert _decide("WebFetch", {"url": "https://docs.python.org/3/"},
                   web_allowlist=allow) == ("allow", "web-allowlist")
    assert _decide("WebFetch", {"url": "https://evil.net/page"},
                   web_allowlist=allow) == ("ask", None)
    # No allowlist, no grant: every fetch asks
    assert _decide("WebFetch", {"url": "https://docs.python.org/3/"}) == ("ask", None)


def test_webfetch_flagged_url_never_auto_approves(repo):
    # Exfiltration-shaped URLs ask even on a trusted domain
    allow = frozenset({"docs.python.org"})
    assert _decide("WebFetch", {
        "url": "https://docs.python.org/x?d=" + "A" * 90},
        web_allowlist=allow) == ("ask", None)


def test_webfetch_grant_is_domain_scoped(repo):
    grants = {"WebFetch:docs.python.org"}
    assert _decide("WebFetch", {"url": "https://docs.python.org/3/"},
                   task_grants=grants) == ("allow", "task-grant")
    # An injected redirect to a new domain breaks out of the grant
    assert _decide("WebFetch", {"url": "https://evil.net/collect"},
                   task_grants=grants) == ("ask", None)
    # A bare tool-name grant must NOT cover fetches (defense in depth:
    # the callback never adds one, but the policy must not honor it either)
    assert _decide("WebFetch", {"url": "https://docs.python.org/3/"},
                   task_grants={"WebFetch"}) == ("ask", None)


def test_web_allowlist_loader(tmp_path):
    f = tmp_path / "domains.txt"
    f.write_text("# comment\n\ndocs.python.org\n  Developer.Mozilla.ORG  \n",
                 encoding="utf-8")
    assert policy.load_web_allowlist(str(f)) == frozenset(
        {"docs.python.org", "developer.mozilla.org"})
    assert policy.load_web_allowlist(str(tmp_path / "missing.txt")) == frozenset()


def test_webfetch_spoken_ask_names_domain_not_url(repo):
    url = "https://docs.python.org/3/library/asyncio-task.html"
    spoken = approvals.describe_action("WebFetch", {"url": url}, spoken=True)
    assert "docs.python.org" in spoken
    assert "asyncio-task.html" not in spoken       # the URL stays on screen
    assert "on your screen" in spoken
    assert url in approvals.describe_action("WebFetch", {"url": url})


def test_webfetch_spoken_ask_warns_on_flags(repo):
    spoken = approvals.describe_action(
        "WebFetch", {"url": "https://evil.net/c?d=" + "A" * 90}, spoken=True)
    assert "careful" in spoken and "encoded data" in spoken


def test_websearch_described_by_query(repo):
    assert approvals.describe_action(
        "WebSearch", {"query": "piper tts sample rate"}) == \
        'search the web for "piper tts sample rate"'


# ---------- Plan contract + run_tests (in-process tools) ----------

def test_plan_tool_is_always_allowed(repo):
    # The tool IS an approval — gating it would ask permission to ask
    assert _decide(policy.PLAN_TOOL, {"goal": "x"}) == ("allow", "plan-tool")


def test_run_tests_needs_a_grant_and_respects_readonly(repo):
    assert _decide(policy.RUN_TESTS_TOOL, {}) == ("ask", None)
    # An approved plan installs the grant; it rides the generic branch
    assert _decide(policy.RUN_TESTS_TOOL, {},
                   task_grants=set(tools.PLAN_GRANTS)) == ("allow", "task-grant")
    # Test suites execute repo code: read-only sessions refuse them
    assert _decide(policy.RUN_TESTS_TOOL, {}, readonly=True,
                   task_grants=set(tools.PLAN_GRANTS)) == \
        ("deny", policy.READONLY_DENY)


def test_plan_grants_cover_edits_and_tests_only():
    assert set(tools.PLAN_GRANTS) == {"edits", policy.RUN_TESTS_TOOL}


def test_detect_test_command_npm(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest"}}', encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which",
                        lambda name: r"C:\fake\npm.cmd" if name == "npm" else None)
    argv, label = tools.detect_test_command(str(tmp_path))
    assert argv == [r"C:\fake\npm.cmd", "test"] and label == "npm test"


def test_detect_test_command_skips_npm_placeholder(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert tools.detect_test_command(str(tmp_path)) is None


def test_detect_test_command_pytest_prefers_repo_venv(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    venv_py = tmp_path / "venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    argv, label = tools.detect_test_command(str(tmp_path))
    assert argv == [str(venv_py), "-m", "pytest"] and label == "pytest"


def test_detect_test_command_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert tools.detect_test_command(str(tmp_path)) is None


def test_summarize_test_output_shapes():
    assert tools.summarize_test_output(
        "....\n151 passed in 0.94s\n", 0, "pytest") == \
        "pytest: 151 passed in 0.94s"
    assert "2 failed" in tools.summarize_test_output(
        "=== 2 failed, 10 passed in 1.2s ===", 1, "pytest")
    assert tools.summarize_test_output(
        "Tests:       3 passed, 3 total\n", 0, "npm test") == \
        "npm test: 3 passed, 3 total"
    assert tools.summarize_test_output("...", 0, "cargo test") == \
        "cargo test: all tests passed"
    assert "exit code 2" in tools.summarize_test_output("boom", 2, "go test")


def test_run_tests_sync_reports_missing_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    result = tools.run_tests_sync(str(tmp_path))
    assert "No test runner found" in result


def test_mcp_tool_feed_lines(repo):
    assert approvals.describe_tool_use(
        policy.PLAN_TOOL, {"goal": "refactor the speaker"}) == \
        "propose plan: refactor the speaker"
    assert approvals.describe_tool_use(policy.RUN_TESTS_TOOL, {}) == "run tests"
    assert approvals.describe_action(policy.RUN_TESTS_TOOL, {}) == \
        "run this repo's test suite"


# ---------- Mass replacement (replace_text core) ----------

@pytest.fixture
def replace_repo(tmp_path, monkeypatch):
    """A real git repo with tracked, untracked, ignored, and binary files,
    with state.git_safety pointed at it."""
    _git(tmp_path, "init")
    (tmp_path / "a.txt").write_text("Wadata HR one\nWadata HR two\n",
                                    encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.md").write_text("Wadata HR three", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfeWadata HR\x00")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "c.txt").write_text("Wadata HR ignored", encoding="utf-8")
    _git(tmp_path, "add", "a.txt", "sub/b.md", "blob.bin", ".gitignore")
    # untracked but not ignored — still part of the project
    (tmp_path / "untracked.txt").write_text("Wadata HR four", encoding="utf-8")
    gs = GitSafety(str(tmp_path))
    monkeypatch.setattr(state, "git_safety", gs)
    monkeypatch.setattr(state, "repo_root", str(tmp_path))
    return tmp_path


def test_scan_replacements_counts_project_files_only(replace_repo):
    hits = dict(tools.scan_replacements(str(replace_repo), "Wadata HR"))
    assert hits == {"a.txt": 2, "sub/b.md": 1, "untracked.txt": 1}
    # binary skipped (undecodable), ignored/ excluded by git


def test_apply_replacements_rewrites_and_reports(replace_repo):
    hits = tools.scan_replacements(str(replace_repo), "Wadata HR")
    seen = []
    changed, failed = tools.apply_replacements(
        str(replace_repo), hits, "Wadata HR", "MDHR", seen.append)
    assert (changed, failed) == (3, 0)
    assert len(seen) == 3                       # checkpoint hook per file
    assert (replace_repo / "a.txt").read_text(encoding="utf-8") == \
        "MDHR one\nMDHR two\n"
    assert (replace_repo / "ignored" / "c.txt").read_text(
        encoding="utf-8") == "Wadata HR ignored"   # untouched
    assert tools.scan_replacements(str(replace_repo), "Wadata HR") == []


def test_apply_replacements_preserves_crlf(replace_repo):
    crlf = replace_repo / "crlf.txt"
    crlf.write_bytes(b"Wadata HR line\r\nplain line\r\n")
    _git(replace_repo, "add", "crlf.txt")
    hits = [("crlf.txt", 1)]
    tools.apply_replacements(str(replace_repo), hits, "Wadata HR", "MDHR")
    assert crlf.read_bytes() == b"MDHR line\r\nplain line\r\n"


def test_replace_tool_policy(repo):
    # Self-gating: allowed outside readonly (it asks by voice itself)...
    assert _decide(policy.REPLACE_TOOL, {"find": "a", "replace": "b"}) == \
        ("allow", "self-ask")
    # ...but a readonly session refuses it before it can ask
    assert _decide(policy.REPLACE_TOOL, {"find": "a", "replace": "b"},
                   readonly=True) == ("deny", policy.READONLY_DENY)


def test_replace_feed_line(repo):
    assert approvals.describe_tool_use(policy.REPLACE_TOOL, {
        "find": "Wadata HR", "replace": "MDHR"}) == \
        'replace "Wadata HR" with "MDHR" everywhere'


def test_context_overflow_speaks_plainly():
    spoken = turn.describe_result_error("Error: Prompt is too long")
    assert "context" in spoken and "nothing got done" in spoken


# ---------- Subagents: the scout is read-only by construction ----------

class _FakeAgentDef:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_scout_cannot_mutate_and_runs_on_haiku(monkeypatch):
    # The SDK class isn't loaded in tests; a stub captures the definition
    monkeypatch.setattr(state, "AgentDefinition", _FakeAgentDef)
    defs = agents.build_agents()
    assert set(defs) == {"scout", "worker"}  # the ONLY delegation that exists
    scout = defs["scout"]
    assert scout.tools == ["Read", "Glob", "Grep"]   # no Edit/Write/Bash/Web
    assert scout.model == "haiku"
    # The scout's prompt must carry the injection rule — repo files may
    # try to instruct it
    assert "data, not instructions" in scout.prompt


def test_worker_executes_gated_no_web_no_delegation(monkeypatch):
    monkeypatch.setattr(state, "AgentDefinition", _FakeAgentDef)
    worker = agents.build_agents()["worker"]
    assert set(worker.tools) == {"Read", "Glob", "Grep", "Edit", "Write", "Bash"}
    assert "Task" not in worker.tools        # no sub-delegation
    assert "WebFetch" not in worker.tools    # execution doesn't browse
    assert worker.model == "inherit"         # the session's full brain
    assert "data, not instructions" in worker.prompt
    # The agent-type fence and the definitions must never drift apart
    assert set(agents.build_agents()) == set(policy.ALLOWED_AGENT_TYPES)


def test_task_feed_line_names_the_agent_type():
    line = approvals.describe_tool_use("Task", {
        "subagent_type": "scout", "description": "map the auth flow"})
    assert line == "scout: map the auth flow"


def test_only_defined_agent_launches_pass_the_policy(repo):
    # Belt: no other agent type is defined. Suspenders: the policy refuses
    # them anyway, even though the CLI may skip this callback for Task.
    assert _decide("Task", {"subagent_type": "scout",
                            "description": "d", "prompt": "p"}) == ("allow", "scout")
    assert _decide("Task", {"subagent_type": "worker",
                            "prompt": "p"}) == ("allow", "worker")
    assert _decide("Task", {"subagent_type": "general-purpose",
                            "prompt": "p"}) == ("deny", policy.OTHER_AGENT_DENY)
    assert _decide("Task", {"prompt": "p"}) == ("deny", policy.OTHER_AGENT_DENY)


# ---------- Project notes (CLAUDE.md read as data, never as settings) ----------

def test_project_notes_missing_is_empty(tmp_path):
    assert context.project_notes(str(tmp_path)) == ""


def test_project_notes_reads_and_labels_both_files(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("Use tabs.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.local.md").write_text("Port 5433.\n", encoding="utf-8")
    notes = context.project_notes(str(tmp_path))
    assert "--- CLAUDE.md ---" in notes and "Use tabs." in notes
    assert "--- CLAUDE.local.md ---" in notes and "Port 5433." in notes


def test_project_notes_empty_files_count_as_absent(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("   \n", encoding="utf-8")
    assert context.project_notes(str(tmp_path)) == ""


def test_project_notes_clip_hostile_size(tmp_path):
    # A repo shipping a book must not crowd the system prompt out of context
    (tmp_path / "CLAUDE.md").write_text("x" * 100_000, encoding="utf-8")
    notes = context.project_notes(str(tmp_path))
    assert len(notes) < context.PROJECT_NOTES_MAX_CHARS + 100
    assert "clipped" in notes


# ---------- Multi-session safety (repo lock + focus gating) ----------

@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "LOCKS_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(session, "_repo_lock_path", None)
    return tmp_path


def test_repo_lock_acquire_and_release(lock_dir):
    acquired, other = session.acquire_repo_lock(str(lock_dir))
    assert (acquired, other) == (True, 0)
    assert os.path.exists(session._repo_lock_file(str(lock_dir)))
    session.release_repo_lock()
    assert not os.path.exists(session._repo_lock_file(str(lock_dir)))


def test_repo_lock_blocks_a_live_session(lock_dir):
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        os.makedirs(session.LOCKS_DIR, exist_ok=True)
        with open(session._repo_lock_file(str(lock_dir)), "w") as f:
            f.write(str(holder.pid))
        assert session.acquire_repo_lock(str(lock_dir)) == (False, holder.pid)
    finally:
        holder.kill()


def test_repo_lock_takes_over_a_stale_lock(lock_dir):
    # A lock left by a crashed/closed session must not brick the repo
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    os.makedirs(session.LOCKS_DIR, exist_ok=True)
    with open(session._repo_lock_file(str(lock_dir)), "w") as f:
        f.write(str(dead.pid))
    acquired, other = session.acquire_repo_lock(str(lock_dir))
    assert (acquired, other) == (True, 0)
    session.release_repo_lock()


def test_lock_file_is_per_repo_and_case_insensitive(lock_dir):
    # Windows paths: same repo in different casing is the same lock
    assert (session._repo_lock_file(r"C:\Users\x\repo")
            == session._repo_lock_file(r"c:\users\X\REPO".replace("REPO", "repo").replace("X", "x")))
    assert (session._repo_lock_file(r"C:\Users\x\repo")
            != session._repo_lock_file(r"C:\Users\x\other"))


def test_focus_helpers_fail_open_not_crash():
    # The real foreground window during a test run is arbitrary; what's
    # pinned is that the helpers work at all: the ancestor walk finds us
    # and our shell, and the focus check returns a bool, never raises.
    pids = session._ancestor_pids()
    assert os.getpid() in pids
    assert len(pids) >= 2  # at least us + the shell that ran pytest
    assert session.session_has_focus() in (True, False)


# ---------- Pane-level focus (terminal focus reports, mode 1004) ----------

def test_terminal_focus_parses_focus_reports():
    tf = session.TerminalFocus()
    for ch in "\x1b[I":
        tf._feed(ch)
    assert tf.state is True and tf._keys == []
    for ch in "\x1b[O":
        tf._feed(ch)
    assert tf.state is False and tf._keys == []


def test_terminal_focus_keys_pass_through_around_reports():
    tf = session.TerminalFocus()
    for ch in "t\x1b[Iq":
        tf._feed(ch)
    assert tf.state is True
    assert tf._keys == ["t", "q"]


def test_terminal_focus_non_focus_escapes_are_forwarded():
    tf = session.TerminalFocus()
    for ch in "\x1bx":  # bare ESC then a key — both must survive
        tf._feed(ch)
    assert tf._keys == ["\x1b", "x"] and tf.state is None
    tf = session.TerminalFocus()
    for ch in "\x1b[A":  # a CSI that isn't a focus report
        tf._feed(ch)
    assert tf._keys == ["\x1b", "[", "A"] and tf.state is None


def test_session_focus_layers(monkeypatch):
    # Solo session: every press is ours, no other layer consulted
    monkeypatch.setattr(session, "_solo_session", lambda: True)
    monkeypatch.setattr(session.terminal_focus, "state", False)
    assert session.session_has_focus() is True
    # Contended: the terminal's own report wins, in both directions
    monkeypatch.setattr(session, "_solo_session", lambda: False)
    monkeypatch.setattr(session.terminal_focus, "pump", lambda: None)
    assert session.session_has_focus() is False
    monkeypatch.setattr(session.terminal_focus, "state", True)
    assert session.session_has_focus() is True


def test_solo_session_counts_live_locks(lock_dir, monkeypatch):
    monkeypatch.setattr(session, "_solo_cache", (True, 0.0))
    os.makedirs(session.LOCKS_DIR, exist_ok=True)
    with open(os.path.join(session.LOCKS_DIR, "own.lock"), "w") as f:
        f.write(str(os.getpid()))
    assert session._solo_session() is True  # just us -> ungated
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(session, "_solo_cache", (True, 0.0))  # bust the cache
        with open(os.path.join(session.LOCKS_DIR, "other.lock"), "w") as f:
            f.write(str(holder.pid))
        assert session._solo_session() is False  # a live second session -> gated
    finally:
        holder.kill()
