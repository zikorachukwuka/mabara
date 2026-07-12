"""Push-to-talk key tracking, pane-focus arbitration, and the per-repo lock.

Everything that decides "does this key press / this repo belong to THIS
process" when several Mabara sessions coexist.
"""

import atexit
import hashlib
import os
import sys
import threading
import time

try:
    import msvcrt  # console key polling for the transcript fold-out
except ImportError:
    msvcrt = None

from . import config
from .config import _USE_COLOR, PUSH_TO_TALK_KEY

# Polling keyboard.is_pressed(PUSH_TO_TALK_KEY) also fired for LEFT ctrl:
# Windows reports scan code 29 for both Ctrl keys (only an "extended" flag
# tells them apart), and the library's name table maps 29 to "right ctrl"
# too — so Ctrl+C/Ctrl+S while typing read as push-to-talk. Key *events* do
# resolve left vs right in their name, so track the key's state from a hook
# (installed via attach_keyboard) and poll that flag instead.
_ptt_down = False

keyboard = None  # the keyboard module, provided by audio._load_audio


def attach_keyboard(module):
    """Called by audio._load_audio once the keyboard library is imported:
    stores the module (for its event constants) and installs the hook.
    Idempotent — both model-loader threads reach _load_audio."""
    global keyboard
    if keyboard is None:
        keyboard = module
        module.hook(_track_ptt)


def _track_ptt(event):
    global _ptt_down
    if event.name and event.name.lower() == PUSH_TO_TALK_KEY:
        # The hook is system-wide; focus decides ownership (see below).
        # Key-up always clears, so losing focus mid-hold can't stick the key.
        if event.event_type == keyboard.KEY_DOWN:
            _ptt_down = session_has_focus()
        else:
            _ptt_down = False


def ptt_pressed():
    return _ptt_down


# The keyboard hook above is system-wide: every Mabara process sees every
# press of the push-to-talk key, so two sessions in two windows both woke
# and answered together. Ownership is decided in layers: a lone session
# takes every press (nothing to arbitrate); otherwise the terminal's own
# focus report decides (mode 1004 — the only signal that can tell two
# VS Code panes apart); terminals that don't send one fall back to the
# window checks (own console focused, or the focused window belongs to an
# ancestor process), which is exactly classic conhost where those work.
_focus_ancestors = None  # ancestor PIDs, computed once on first key press


def _ancestor_pids():
    """Our PID plus every ancestor's, via a Toolhelp32 process snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    k32 = ctypes.windll.kernel32
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    parent_of = {}
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    if k32.Process32First(snapshot, ctypes.byref(entry)):
        while True:
            parent_of[entry.th32ProcessID] = entry.th32ParentProcessID
            if not k32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    k32.CloseHandle(snapshot)

    pids = set()
    pid = os.getpid()
    while pid and pid not in pids:  # PID reuse can make the map cyclic
        pids.add(pid)
        pid = parent_of.get(pid, 0)
    return pids


class TerminalFocus:
    """Pane-level focus, reported by the terminal itself (xterm mode 1004).

    The window checks in session_has_focus can't see INSIDE a terminal
    host: every VS Code terminal — tab, split, even a second VS Code
    window — belongs to the same Code.exe, so two sessions both looked
    focused and answered in chorus (observed live 2026-07-07). With focus
    reporting enabled, VS Code and Windows Terminal write ESC[I / ESC[O
    to stdin when the pane gains/loses focus. Classic conhost never sends
    one, which leaves `state` at None and the window checks in charge —
    the one host where they actually work.

    This class owns ALL console-input reading: focus sequences update
    `state`, every other key is queued for the fold-out's poll. One
    reader, because two consumers of msvcrt steal each other's bytes."""

    def __init__(self):
        self.state = None   # None until the terminal proves it speaks 1004
        self._keys = []
        self._esc = ""      # partially received escape sequence
        self._lock = threading.Lock()
        self._enabled = False
        self._conin = None      # CONIN$ handle while VT input mode is ours
        self._prev_mode = None  # console input mode to restore on exit

    def enable(self):
        # Only where ANSI goes through at all — and never before the last
        # input(): an alt-tab would type ESC[O into the resume answer.
        if msvcrt is None or not _USE_COLOR:
            return
        # Asking the terminal to send focus reports (1004h below) is only
        # half the job: under ConPTY (Windows Terminal, VS Code) conhost
        # SWALLOWS the incoming ESC[I/O — they arrive neither as characters
        # nor as FOCUS_EVENT records (verified with a pseudoconsole probe on
        # this machine, 2026-07-07). ENABLE_VIRTUAL_TERMINAL_INPUT makes
        # conhost pass them through as characters for pump() to parse.
        # Plain keys ('t') still arrive as themselves, and Ctrl+C keeps
        # signaling because ENABLE_PROCESSED_INPUT stays set.
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            conin = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
            if conin not in (None, -1):
                mode = wintypes.DWORD()
                if k32.GetConsoleMode(conin, ctypes.byref(mode)):
                    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
                    if k32.SetConsoleMode(
                            conin,
                            mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT):
                        self._conin = conin
                        self._prev_mode = mode.value
        except Exception:
            pass  # fail open: focus reports just won't arrive
        sys.stdout.write("\x1b[?1004h")
        sys.stdout.flush()
        self._enabled = True
        atexit.register(self.disable)

    def disable(self):
        # Left on after exit, 1004 sprays ESC[I/O into the shell on every
        # alt-tab. Idempotent: the Ctrl+C path os._exit()s past atexit and
        # calls this directly.
        if not self._enabled:
            return
        self._enabled = False
        try:
            sys.stdout.write("\x1b[?1004l")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        if self._conin is not None:
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleMode(
                    self._conin, self._prev_mode)
                ctypes.windll.kernel32.CloseHandle(self._conin)
            except Exception:
                pass
            self._conin = None

    def _feed(self, ch):
        """One character of console input. Focus reports update state;
        anything else — including an abandoned escape prefix — passes
        through to the key queue untouched."""
        if self._esc:
            candidate = self._esc + ch
            if candidate in ("\x1b[I", "\x1b[O"):
                self.state = candidate == "\x1b[I"
                self._esc = ""
            elif "\x1b[I".startswith(candidate) or "\x1b[O".startswith(candidate):
                self._esc = candidate
            else:
                self._keys.extend(candidate[:-1])
                self._esc = ""
                self._feed(ch)  # re-examine: could start a new sequence
            return
        if ch == "\x1b":
            self._esc = ch
        else:
            self._keys.append(ch)

    def pump(self):
        """Parse everything currently buffered in the console."""
        if msvcrt is None:
            return
        with self._lock:
            try:
                while msvcrt.kbhit():
                    self._feed(msvcrt.getwch())
            except OSError:
                pass

    def take_keys(self):
        """Pending non-focus keys, for whoever reads the keyboard."""
        self.pump()
        with self._lock:
            keys, self._keys = self._keys, []
        return keys

    def discard_keys(self):
        """Drop buffered keystrokes but keep what they said about focus —
        stray typing must not toggle anything later."""
        self.take_keys()


terminal_focus = TerminalFocus()

# Solo-session cache: gating only matters when a second session could
# answer too. (value, checked_at) — recounted at most every few seconds,
# because this runs on every key press.
_solo_cache = (True, 0.0)


def _solo_session():
    """True when no OTHER live Mabara holds a repo lock. A lone session
    keeps the ungated behavior — the key works no matter which window has
    focus, so glancing at an editor or browser never deadens push-to-talk."""
    global _solo_cache
    value, checked = _solo_cache
    if time.time() - checked < 3.0:
        return value
    count = 0
    try:
        for name in os.listdir(LOCKS_DIR):
            try:
                with open(os.path.join(LOCKS_DIR, name), encoding="utf-8") as f:
                    pid = int(f.read().strip() or 0)
            except (OSError, ValueError):
                continue
            if pid == os.getpid() or _pid_running(pid):
                count += 1
    except OSError:
        pass
    value = count <= 1
    _solo_cache = (value, time.time())
    return value


def session_has_focus():
    """Does this press of the push-to-talk key belong to this session?
    Solo sessions always say yes; otherwise the terminal's own focus
    report decides, then the window checks. Fails open — if any layer
    breaks, behavior is the old single-session behavior rather than a
    dead push-to-talk key."""
    global _focus_ancestors
    try:
        if _solo_session():
            return True
        terminal_focus.pump()
        if terminal_focus.state is not None:
            return terminal_focus.state
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        foreground = user32.GetForegroundWindow()
        if not foreground:
            return True
        console = kernel32.GetConsoleWindow()
        if console and foreground == console:
            return True
        if _focus_ancestors is None:
            _focus_ancestors = _ancestor_pids()
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
        return owner.value in _focus_ancestors
    except Exception:
        return True


# ---------- One session per repo (lockfile) ----------
# Two Mabara sessions on the SAME repo would fight over checkpoints,
# session state, and the transcript — that's blocked outright at startup.
# Different repos in different windows coexist fine (session_has_focus
# decides who owns the push-to-talk key), so the lock is per repo path.
LOCKS_DIR = os.path.join(config.DATA_DIR, "locks")
_repo_lock_path = None  # held lock, released at exit


def _repo_lock_file(repo_path):
    digest = hashlib.sha256(
        os.path.normcase(repo_path).encode("utf-8")).hexdigest()[:16]
    return os.path.join(LOCKS_DIR, digest + ".lock")


def _pid_running(pid):
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def acquire_repo_lock(repo_path):
    """Claim this repo for the current process. Returns (True, 0) on
    success, (False, other_pid) when a live session already holds it.
    A lock left by a dead process (crash, force-close) is taken over."""
    global _repo_lock_path
    os.makedirs(LOCKS_DIR, exist_ok=True)
    path = _repo_lock_file(repo_path)
    try:
        with open(path, encoding="utf-8") as f:
            other = int(f.read().strip() or 0)
    except (OSError, ValueError):
        other = 0
    if other and other != os.getpid() and _pid_running(other):
        return (False, other)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    _repo_lock_path = path
    atexit.register(release_repo_lock)
    return (True, 0)


def release_repo_lock():
    """Remove the lock if this process still owns it. Safe to call twice —
    both atexit and the Ctrl+C handler (which os._exit()s past atexit)
    come through here."""
    global _repo_lock_path
    if not _repo_lock_path:
        return
    try:
        with open(_repo_lock_path, encoding="utf-8") as f:
            owner = int(f.read().strip() or 0)
        # Remove only after the handle is closed — Windows can't delete
        # an open file, and the swallowed PermissionError would leave the
        # lock behind for the liveness check to clean up a session later.
        if owner == os.getpid():
            os.remove(_repo_lock_path)
    except (OSError, ValueError):
        pass
    _repo_lock_path = None
