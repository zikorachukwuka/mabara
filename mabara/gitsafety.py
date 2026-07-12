"""Git safety net: per-task checkpoints, voice-driven revert and commit."""

import difflib
import os
import re
import shutil
import subprocess

from .policy import _path_within
from .terminal import red


class GitSafety:
    """Edits are only allowed inside a git repository, and every task that
    touches files gets a checkpoint: a `git stash create` snapshot of the
    working tree, taken lazily at the first approved edit, plus in-memory
    backups of untracked files (which a stash snapshot doesn't cover — a
    naive revert would delete the user's own uncommitted new files).
    'Revert that' restores everything the last edit-task touched."""

    def __init__(self, repo_path):
        self.repo = repo_path
        # Resolve git once and by absolute path. Windows' executable search
        # includes the current directory, and mabara is typically launched
        # from inside the repo it's pointed at — an untrusted repo could
        # plant its own git.exe at its root. Refuse any git that lives
        # inside the target repo.
        exe = shutil.which("git")
        if exe and _path_within(exe, repo_path):
            exe = None
        self._git_exe = exe
        proc = self._git("rev-parse", "--is-inside-work-tree")
        self.enabled = proc is not None and proc.stdout.strip() == "true"
        status = self._git("status", "--porcelain") if self.enabled else None
        self.dirty = bool(status.stdout.strip()) if status else False
        self._turn = 0
        self._ckpt_turn = None   # turn the current checkpoint belongs to
        self._baseline = None    # stash-create sha; None means use HEAD
        self._head_at_ckpt = True  # False: repo had no commits at checkpoint time
        self._touched = []       # absolute paths of approved Edit/Write targets
        self._untracked_backup = {}  # path -> original bytes
        self._bash_ran = False
        self._task_label = ""    # user's words for the task that got the checkpoint
        self._pending_label = ""

    def _git(self, *args):
        if self._git_exe is None:
            return None
        try:
            return subprocess.run(
                [self._git_exe, "-C", self.repo, *args],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _is_tracked(self, path):
        proc = self._git("ls-files", "--error-unmatch", "--", path)
        return proc is not None and proc.returncode == 0

    def recheck(self):
        """Re-detect the repo when edits are blocked. `enabled` is a startup
        snapshot, but the deny message itself tells the user that git init
        fixes the block — a cached False must not outlive the fact it
        describes. (Live failure 2026-07-05: the agent ran the suggested git
        init, kept being told 'not a git repository', concluded Edit/Write
        were broken, and routed around the gate with a shell heredoc.)"""
        if not self.enabled:
            proc = self._git("rev-parse", "--is-inside-work-tree")
            self.enabled = proc is not None and proc.stdout.strip() == "true"
        return self.enabled

    def begin_turn(self, label=""):
        self._turn += 1
        self._pending_label = label

    def before_mutation(self, tool_name, tool_input):
        """Called when the user approves a mutating tool. Snapshots the tree
        once per turn and remembers which files the task touches. Returns
        True when this call took the snapshot, so the caller can announce
        it — a safety net nobody can see earns no trust."""
        if not self.enabled:
            return False
        created = False
        if self._ckpt_turn != self._turn:
            self._ckpt_turn = self._turn
            # A repo with no commits yet (fresh git init) has no HEAD: stash
            # create fails and 'checkout HEAD' can't restore anything, so the
            # byte backups below must cover every touched file, not just the
            # untracked ones.
            head = self._git("rev-parse", "--verify", "--quiet", "HEAD")
            self._head_at_ckpt = head is not None and head.returncode == 0
            self._baseline = None
            if self._head_at_ckpt:
                proc = self._git("stash", "create", "mabara checkpoint")
                self._baseline = (proc.stdout.strip() or None) if proc else None
            self._touched = []
            self._untracked_backup = {}
            self._bash_ran = False
            self._task_label = self._pending_label
            created = True
        if tool_name in ("Edit", "Write"):
            path = tool_input.get("file_path")
            if path:
                path = os.path.abspath(path)
                if path not in self._touched:
                    self._touched.append(path)
                    if os.path.exists(path) and (
                            not self._head_at_ckpt or not self._is_tracked(path)):
                        try:
                            with open(path, "rb") as f:
                                self._untracked_backup[path] = f.read()
                        except OSError:
                            pass
        elif tool_name == "Bash":
            self._bash_ran = True
        return created

    def revert(self):
        """Undo the last edit-task. Returns a short spoken summary."""
        if not self.enabled:
            return "There's no safety net here — this folder isn't a git repository."
        if not self._touched and not self._bash_ran:
            return "There's nothing to revert."
        baseline = self._baseline or "HEAD"
        restored = removed = failed = 0
        for path in self._touched:
            rel = os.path.relpath(path, self.repo)
            if path in self._untracked_backup:
                try:
                    with open(path, "wb") as f:
                        f.write(self._untracked_backup[path])
                    restored += 1
                except OSError:
                    failed += 1
                continue
            if not self._head_at_ckpt:
                # No commits at checkpoint time: every file that existed then
                # got a byte backup above, so anything left was created by
                # the task — undoing means deleting it.
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        removed += 1
                except OSError:
                    failed += 1
                continue
            proc = self._git("checkout", baseline, "--", rel)
            if proc is not None and proc.returncode == 0:
                restored += 1
            elif not self._is_tracked(path) and os.path.exists(path):
                # File didn't exist at the checkpoint: it was created by the
                # task, so undoing means deleting it
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    failed += 1
            else:
                failed += 1
        parts = []
        if restored:
            parts.append(f"restored {restored} file" + ("s" if restored != 1 else ""))
        if removed:
            parts.append(f"removed {removed} new file" + ("s" if removed != 1 else ""))
        if failed:
            parts.append(f"couldn't revert {failed}")
        message = ("Done — " + " and ".join(parts) + ".") if parts \
            else "That task didn't change any files."
        if self._bash_ran:
            message += " Note: shell commands from that task can't be undone automatically."
        # One-shot: a second 'revert that' shouldn't re-fire on stale state
        self._touched = []
        self._untracked_backup = {}
        self._bash_ran = False
        self._ckpt_turn = None
        return message

    def commit_preview(self):
        """(paths, subject) for a would-be commit of the last edit-task's
        files, or None if there's nothing to commit."""
        if not self.enabled or not self._touched:
            return None
        paths = [p for p in self._touched if os.path.exists(p)]
        if not paths:
            return None
        label = re.sub(r"\s+", " ", self._task_label).strip().rstrip(".?!")
        subject = f"mabara: {label[:60]}" if label else "mabara: voice task changes"
        return paths, subject

    def commit(self, subject):
        """Commit only the last task's touched files (never the user's own
        unrelated changes). Returns a short spoken outcome."""
        preview = self.commit_preview()
        if preview is None:
            return "There's nothing to commit."
        paths, _ = preview
        rels = [os.path.relpath(p, self.repo) for p in paths]
        self._git("add", "--", *rels)
        proc = self._git("commit", "-m", subject, "--", *rels)
        if proc is None or proc.returncode != 0:
            detail = ((proc.stderr or proc.stdout).strip() if proc else "git unavailable")
            print(f"  {red('!')} {detail[:200]}")
            return "The commit failed — details are on your screen."
        # Committed work is no longer checkpoint-revertable state
        self._touched = []
        self._untracked_backup = {}
        self._bash_ran = False
        self._ckpt_turn = None
        n = len(rels)
        return f"Committed {n} file" + ("s." if n != 1 else ".")

    def turn_diffstat(self):
        """Per-file (relpath, added, removed) line counts for the files this
        turn's task touched — the receipt of what actually changed, in the
        diffstat shape git taught everyone to read. Empty when the current
        turn made no checkpointed edits."""
        if not self.enabled or self._ckpt_turn != self._turn:
            return []
        baseline = self._baseline or "HEAD"
        stats = []
        for path in self._touched:
            # Forward slashes: backslashes are escape characters in git
            # pathspecs, so a raw Windows relpath can silently match nothing
            rel = os.path.relpath(path, self.repo).replace("\\", "/")
            # With no HEAD at checkpoint time git diff has nothing to diff
            # against — the byte backups below carry the whole receipt.
            proc = (self._git("diff", "--numstat", baseline, "--", rel)
                    if self._head_at_ckpt else None)
            out = proc.stdout.strip() if proc and proc.returncode == 0 else ""
            if out:
                added, removed = out.split("\t")[:2]
                if added != "-":  # binary files have no line counts
                    stats.append((rel, int(added), int(removed)))
                continue
            if self._head_at_ckpt and self._is_tracked(path):
                continue  # tracked and unchanged vs the checkpoint
            # Untracked files are invisible to git diff: count by hand
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    new_lines = f.read().splitlines()
            except OSError:
                continue
            if path in self._untracked_backup:
                old_lines = self._untracked_backup[path].decode(
                    "utf-8", "replace").splitlines()
                added = removed = 0
                for line in difflib.unified_diff(old_lines, new_lines,
                                                 lineterm="", n=0):
                    if line.startswith("+") and not line.startswith("+++"):
                        added += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        removed += 1
                stats.append((rel, added, removed))
            else:
                stats.append((rel, len(new_lines), 0))
        return stats
