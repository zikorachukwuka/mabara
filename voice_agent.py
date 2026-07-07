import asyncio
import atexit
import hashlib
import re
import difflib
import time
import queue
import random
import signal
import threading
import subprocess
import shutil
import sys
import json
import os
import argparse
import textwrap
try:
    import msvcrt  # console key polling for the transcript fold-out
except ImportError:
    msvcrt = None
# All models are cached locally, so skip HuggingFace Hub's startup network
# checks. If you ever switch to a Whisper model you haven't downloaded yet,
# run once with HF_HUB_OFFLINE=0 in the environment to allow the download.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# The Claude Agent SDK costs ~1.4s to import, and sounddevice + numpy +
# keyboard another ~1.1s — together that was the blank-terminal time before
# the banner's first frame. main() loads both sets in background threads
# (alongside the STT/TTS models) while the banner draws; nothing below
# touches these names until the matching loader has finished.
# faster-whisper and piper are likewise imported inside the engine classes
# that use them, off the main thread.
ClaudeSDKClient = None
ClaudeAgentOptions = None
PermissionResultAllow = None
PermissionResultDeny = None
StreamEvent = None


def _load_sdk():
    global ClaudeSDKClient, ClaudeAgentOptions, \
        PermissionResultAllow, PermissionResultDeny, StreamEvent
    import claude_agent_sdk as sdk
    ClaudeSDKClient = sdk.ClaudeSDKClient
    ClaudeAgentOptions = sdk.ClaudeAgentOptions
    PermissionResultAllow = sdk.PermissionResultAllow
    PermissionResultDeny = sdk.PermissionResultDeny
    StreamEvent = sdk.StreamEvent


sd = None        # sounddevice
np = None        # numpy
keyboard = None

_audio_import_lock = threading.Lock()


def _load_audio():
    """Import the audio stack (sounddevice, numpy, keyboard). Idempotent and
    locked: both model-loader threads call it, the first one pays."""
    global sd, np, keyboard
    with _audio_import_lock:
        if np is None:
            import numpy
            import sounddevice
            import keyboard as _keyboard
            np = numpy
            sd = sounddevice
            keyboard = _keyboard
            keyboard.hook(_track_ptt)


SAMPLERATE = 16000
# Recordings shorter than this are a key tap (pre-roll only), not speech
MIN_SPEECH_SECONDS = 0.4
# Right Ctrl instead of space: space made typing impossible while Mabara
# talks (any space bar press triggered barge-in). Right Ctrl is never part
# of normal typing (shortcuts live on left Ctrl) and is comfortable to hold.
PUSH_TO_TALK_KEY = "right ctrl"
PTT_LABEL = "RIGHT CTRL"  # how the key is written in on-screen hints

# Polling keyboard.is_pressed(PUSH_TO_TALK_KEY) also fired for LEFT ctrl:
# Windows reports scan code 29 for both Ctrl keys (only an "extended" flag
# tells them apart), and the library's name table maps 29 to "right ctrl"
# too — so Ctrl+C/Ctrl+S while typing read as push-to-talk. Key *events* do
# resolve left vs right in their name, so track the key's state from a hook
# (installed in _load_audio) and poll that flag instead.
_ptt_down = False


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

    def enable(self):
        # Only where ANSI goes through at all — and never before the last
        # input(): an alt-tab would type ESC[O into the resume answer.
        if msvcrt is None or not _USE_COLOR:
            return
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
_HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(_HERE, "models")   # downloaded TTS models/voices
DATA_DIR = os.path.join(_HERE, "data")       # runtime state (sessions, transcripts)
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_STORE_FILE = os.path.join(DATA_DIR, "sessions.json")

# ---------- One session per repo (lockfile) ----------
# Two Mabara sessions on the SAME repo would fight over checkpoints,
# session state, and the transcript — that's blocked outright at startup.
# Different repos in different windows coexist fine (session_has_focus
# decides who owns the push-to-talk key), so the lock is per repo path.
LOCKS_DIR = os.path.join(DATA_DIR, "locks")
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
TRANSCRIPT_FILE = os.path.join(DATA_DIR, "transcripts.log")
# fp32 on purpose: on CPUs without VNNI (like this one) the int8 model
# benchmarks ~2.5x SLOWER than fp32, not faster.
KOKORO_MODEL_FILE = os.path.join(MODELS_DIR, "kokoro-v1.0.onnx")
KOKORO_VOICES_FILE = os.path.join(MODELS_DIR, "voices-v1.0.bin")
TTS_VOICE = "af_heart"
# User's pick after A/B (liked the accent). Community-ranked alternatives
# downloaded alongside: en_US-hfc_male-medium ("cleanest male in the
# catalog"), en_US-amy-medium (female). joe has no high/low variants.
PIPER_DEFAULT_VOICE = "en_US-joe-medium"
PIPER_LENGTH_SCALE = 0.95  # <1.0 speaks faster; stock pacing sounds drawly
# User's pick after listening to M1-M5 at 1.15x pacing. 8 steps = full
# quality at 2.1x real-time on this CPU. Bonus: M1 is the voice that
# survives 4 steps (4.2x) with little quality loss, so if speed is ever
# needed again, drop SUPERTONIC_STEPS to 4.
SUPERTONIC_VOICE = "M1"
SUPERTONIC_STEPS = 5  # walked up from the 4-step floor: 5 beat both 4
                      # (quality) and 6 (speed, with no clear quality win
                      # over 5) on a live listen. Still short of Piper's
                      # snappiness — under longer-term real-use review.
SUPERTONIC_SPEED = 1.22  # >1 speaks faster; package default 1.05 felt slow


# ---------- Terminal styling ----------

os.system("")  # switches Windows consoles into ANSI escape mode
_USE_COLOR = sys.stdout.isatty()


def _style(code):
    def apply(text):
        if not _USE_COLOR:
            return str(text)
        return f"\033[{code}m{text}\033[0m"
    return apply


dim = _style("2")
cyan = _style("1;96")    # the user
accent = _style("1;95")  # Mabara
yellow = _style("1;93")
green = _style("92")
red = _style("91")


def _safe_glyph(char, fallback):
    """Degrade to ASCII when stdout can't encode the pretty glyph
    (e.g. piped output on Windows uses cp1252, not the console's UTF-8)."""
    try:
        char.encode(sys.stdout.encoding or "utf-8")
        return char
    except (UnicodeEncodeError, LookupError):
        return fallback


# Stick to CP437-era glyphs — fancier ones (⋮ ✓) encode fine but render as
# boxes in classic Windows console fonts.
DOT = _safe_glyph("●", "*")
TOOL_MARK = _safe_glyph("·", "|")   # tool-action lines
CHECK = _safe_glyph("√", "+")       # end-of-task marker
SUB_MARK = _safe_glyph("♪", ">")    # live speech subtitle

_LINE_WIDTH = 72


def status(text):
    """Overwrite the single in-place status line (no newline)."""
    print("\r" + " " * _LINE_WIDTH + f"\r  {text}", end="", flush=True)


def clear_status():
    print("\r" + " " * _LINE_WIDTH + "\r", end="", flush=True)

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


# ---------- CLI args ----------

def parse_args():
    parser = argparse.ArgumentParser(description="Voice coding assistant")
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the codebase you want to talk about (default: current directory)",
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Starting Claude model: an alias (sonnet, haiku, opus) or a full "
             "model id, e.g. claude-sonnet-5. Switch anytime by voice: "
             "'switch to haiku' stretches your usage quota for casual "
             "sessions, 'switch to sonnet' restores quality (default: sonnet)",
    )
    parser.add_argument(
        "--stt",
        default="parakeet",
        choices=["parakeet", "small.en", "distil-small.en", "base.en"],
        help="Transcription model: parakeet (nvidia parakeet-tdt-0.6b-v2) is "
             "both faster and more accurate than the whisper options on this "
             "machine (default: parakeet)",
    )
    parser.add_argument(
        "--tts",
        default="piper",
        choices=["piper", "supertonic", "kokoro"],
        help="Voice engine: piper is the snappy default (~0.4s to first "
             "word); supertonic sounds more natural but takes ~1.6s to start "
             "speaking; kokoro can't keep up on this CPU (default: piper)",
    )
    parser.add_argument(
        "--voice",
        default=PIPER_DEFAULT_VOICE,
        help="Piper voice name; its .onnx must be in the models folder. "
             "Downloaded: en_US-hfc_male-medium, en_US-joe-medium, "
             "en_US-amy-medium. Ignored with --tts kokoro. "
             f"(default: {PIPER_DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show per-turn timing (transcription, Claude first token) to "
             "diagnose where response lag comes from",
    )
    parser.add_argument(
        "--readonly",
        action="store_true",
        help="Look-don't-touch session: Edit, Write, and Bash are refused "
             "outright, no approval prompts — nothing in the repo can change",
    )
    return parser.parse_args()


# ---------- Session persistence ----------

def load_sessions():
    if not os.path.exists(SESSION_STORE_FILE):
        return {}
    try:
        with open(SESSION_STORE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_session(repo_path, session_id):
    sessions = load_sessions()
    sessions[repo_path] = session_id
    # Write-then-rename: a crash mid-write must not corrupt the store —
    # load_sessions answers a corrupt file by silently forgetting every
    # saved conversation.
    tmp_file = SESSION_STORE_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(sessions, f, indent=2)
    os.replace(tmp_file, SESSION_STORE_FILE)


def prompt_resume(session_id):
    """Enter defaults to yes — if you're being asked at all, you were in the
    middle of something on this repo last time.

    The question is printed via sys.stdout and input() is called BARE:
    input(prompt) hands the prompt to the C runtime's console readline
    path rather than sys.stdout, and on this setup that write never
    reached the screen — the app sat waiting on an invisible question."""
    print(f"  {accent(DOT)} pick up where you left off? {dim('(Y/n)')} ",
          end="", flush=True)
    try:
        answer = input()
    except EOFError:
        return False
    return not answer.strip().lower().startswith("n")


# Rotation cap: transcripts hold everything both sides say, in plaintext,
# forever — bound the exposure (and the disk) at ~2x this across the live
# file and one .1 backup instead of growing without limit.
TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024

# Running line count of TRANSCRIPT_FILE, so each entry can be referenced as
# a clickable file:line in the terminal. Counted once lazily, then kept in
# step by counting the newlines actually written; rotation resets it.
_transcript_lines = None


def _count_file_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def append_transcript(role, text):
    """The terminal only shows subtitles while Mabara speaks; the full prose
    lands here so it can always be re-read. Returns the 1-based line number
    where the entry starts, or None if the write failed."""
    global _transcript_lines
    if not text:
        return None
    try:
        if (os.path.exists(TRANSCRIPT_FILE)
                and os.path.getsize(TRANSCRIPT_FILE) >= TRANSCRIPT_MAX_BYTES):
            os.replace(TRANSCRIPT_FILE, TRANSCRIPT_FILE + ".1")
            _transcript_lines = 0
        if _transcript_lines is None:
            _transcript_lines = _count_file_lines(TRANSCRIPT_FILE)
        entry = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {role}: {text}\n"
        with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        first_line = _transcript_lines + 1
        # Code blocks carry their own newlines, so an entry can span lines
        _transcript_lines += entry.count("\n")
        return first_line
    except OSError:
        return None


# ---------- Last-reply fold-out (press T while idle) ----------

class LastTurnView:
    """Reread the reply you just heard: pressing T between turns prints the
    sentences under the summary line; pressing T again folds them away.

    The fold works by erasing upward with ANSI — possible only while the
    block is still the newest thing on screen, which the idle loop
    guarantees (nothing else prints while waiting for push-to-talk). The
    moment a new turn starts the block is surrendered to scrollback and
    the toggle state resets. Older turns live in transcripts.log; the
    footer prints that path with the entry's line number, which VS Code
    and Windows Terminal render as a clickable link."""

    def __init__(self):
        self._lines = []       # last turn's sentences / code placeholders
        self._log_line = None  # entry's 1-based line in transcripts.log
        self._rows = 0         # terminal rows printed while unfolded
        self._unfolded = False

    def set_turn(self, lines, log_line):
        self._lines = [line for line in lines if line.strip()]
        self._log_line = log_line
        self._rows = 0
        self._unfolded = False

    def drain(self):
        """Discard console keys buffered before the idle wait began, so a
        stray keystroke made while Mabara was talking doesn't count as a
        toggle (and never leaks into a later input()). Goes through the
        focus tracker — the only console reader — so any focus reports
        hiding in the backlog still update the state they describe."""
        terminal_focus.discard_keys()

    def poll(self, prompt):
        """One idle-loop tick: consume pending console keys, toggle on T.
        Console input only carries keys typed into THIS pane, so unlike
        the global push-to-talk hook it needs no focus arbitration."""
        pressed = False
        skip_next = False
        for ch in terminal_focus.take_keys():
            if skip_next:  # second half of an arrow/F-key event pair
                skip_next = False
                continue
            if ch in ("\x00", "\xe0"):
                skip_next = True
                continue
            if ch in ("t", "T"):
                pressed = True
        if pressed and self._lines:
            if self._unfolded:
                self._fold()
            else:
                self._unfold()
            status(dim(f"» {prompt}"))

    def leave_idle(self):
        """Idle is ending: new output will print below the block, so it can
        no longer be erased. Leave it on screen and forget it."""
        self._rows = 0
        self._unfolded = False

    def _unfold(self):
        size = shutil.get_terminal_size((80, 24))
        wrap_width = max(20, min(size.columns - 6, 92))
        body = []
        for line in self._lines:
            body.extend(textwrap.wrap(line, wrap_width) or [line])
        # Cap to the viewport: rows that scroll off the top can't be erased
        # on fold and would be left behind as artifacts.
        max_body = max(4, size.lines - 5)
        clipped = len(body) > max_body
        if clipped:
            body = body[-max_body:]

        def row(text):
            # One row per print — folding counts prints to know what to
            # erase, so no printed line may wrap. (Resizing the terminal
            # between unfold and fold can still break the count; rare.)
            limit = max(10, size.columns - 3)
            if len(text) > limit:
                text = text[:limit - 1] + "…"
            print(f"  {dim(text)}")

        clear_status()
        rule = "-" * min(46, max(12, size.columns - 4))
        header = ("--[ last reply (end — full text in the log) ]" if clipped
                  else "--[ last reply ]")
        row(header + rule[len(header):])
        for text in body:
            row("  " + text)
        row(rule)
        ref = TRANSCRIPT_FILE + (f":{self._log_line}" if self._log_line else "")
        # Clip the ref from the LEFT: the filename and line number are the
        # part worth keeping (and clicking) when the terminal is narrow.
        limit = max(10, size.columns - 5)
        if len(ref) > limit:
            ref = "…" + ref[-(limit - 1):]
        row(ref)
        self._rows = len(body) + 3
        self._unfolded = True

    def _fold(self):
        clear_status()
        if _USE_COLOR:
            # Erase upward through everything _unfold printed; the cursor
            # lands where the header was, ready for the status line.
            sys.stdout.write("\033[A\033[2K" * self._rows)
            sys.stdout.flush()
        self._rows = 0
        self._unfolded = False


last_turn_view = LastTurnView()


# ---------- Git safety net (checkpoints + voice revert) ----------

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


_COMMIT_WORDS = {
    "commit", "that", "this", "it", "them", "these", "the", "change",
    "changes", "task", "tasks", "work", "edit", "edits", "please", "now",
}


def is_commit_command(text):
    """True only for short, unambiguous commands like 'commit this' —
    questions about commits go to Claude as normal conversation."""
    words = re.findall(r"[a-z']+", text.lower())
    return (bool(words) and words[0] == "commit"
            and len(words) <= 6 and all(w in _COMMIT_WORDS for w in words))


_MODEL_ALIASES = {
    "sonnet": "sonnet", "sonet": "sonnet", "sonnets": "sonnet",
    "haiku": "haiku", "opus": "opus",
}


def normalize_model_arg(raw):
    """--model accepts a bare alias ('sonnet') — always resolving to
    whatever that family's current default is — or a full model id
    ('claude-sonnet-5') to pin an exact version. Only fixes up spelling of
    the bare alias itself (e.g. 'sonet' -> 'sonnet'); a version number
    appended to the word (e.g. 'sonnet5') is deliberately NOT stripped or
    guessed at here, because 'which version is current' changes over time
    and hardcoding it just goes stale the next time a model ships. That
    ambiguity is instead caught and explained at startup, once, in
    main() — see the --model validation right after parse_args()."""
    key = raw.strip().lower()
    return _MODEL_ALIASES.get(key, raw)


_SWITCH_FILLER = {
    "switch", "use", "change", "to", "the", "model", "brain",
    "please", "now", "over",
}


def model_switch_target(text):
    """'switch to sonnet' → 'sonnet'; None for anything that isn't a short,
    unambiguous switch command (questions about models go to Claude)."""
    words = re.findall(r"[a-z']+", text.lower())
    if not words or words[0] not in ("switch", "use", "change") or len(words) > 6:
        return None
    models = [_MODEL_ALIASES[w] for w in words if w in _MODEL_ALIASES]
    if len(models) != 1:
        return None
    if all(w in _SWITCH_FILLER or w in _MODEL_ALIASES for w in words):
        return models[0]
    return None


_REVERT_WORDS = {
    "revert", "undo", "that", "this", "it", "everything", "all", "your",
    "the", "last", "change", "changes", "edit", "edits", "task", "tasks",
    "please", "now",
}


def is_revert_command(text):
    """True only for short, unambiguous commands like 'revert that' or
    'undo your last changes' — anything wordier goes to Claude as usual."""
    words = re.findall(r"[a-z']+", text.lower())
    return (bool(words) and words[0] in ("revert", "undo")
            and len(words) <= 6 and all(w in _REVERT_WORDS for w in words))


_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "go", "ahead",
    "approve", "approved", "affirmative", "do", "alright", "fine",
    "absolutely", "definitely", "course",
}
_NO_WORDS = {
    "no", "nope", "nah", "not", "don't", "dont", "stop", "deny", "denied",
    "decline", "cancel", "never", "negative", "wait", "hold",
}
# Words allowed to ride along with a yes word without breaking the approval
# ("yes please do it", "yes for the whole task"). Anything outside this
# vocabulary means the answer carries more than an approval — a question,
# a condition, a new instruction — and the gate fails closed.
_FILLER_WORDS = {
    "please", "it", "it's", "that", "that's", "this", "them", "then",
    "now", "the", "a", "for", "to", "of", "all", "whole", "task",
    "everything",
    # First-person approvals: "yes, I approve", "I said yes". Without
    # these, the most natural way to say yes failed the closed-vocabulary
    # gate and was denied — observed live, twice in one approval storm.
    "i", "i'm", "said",
}


def is_affirmative(answer):
    """Spoken yes/no for approvals. Matches whole words, never substrings
    ('ok' must not fire inside 'look' or 'broken'), and fails closed three
    ways: any deny word vetoes the whole answer ('yes— wait, no' is a no);
    the answer must contain a yes word; and every word must come from the
    closed approval vocabulary, so a question or hesitation that happens to
    contain a yes word ('what will this do', 'okay, show me the diff
    first') never approves — the words outside the vocabulary prove the
    answer is not just an approval."""
    words = re.findall(r"[a-z']+", answer.lower())
    if not words or set(words) & _NO_WORDS:
        return False
    if not set(words) & _YES_WORDS:
        return False
    return all(w in _YES_WORDS or w in _FILLER_WORDS for w in words)


def is_plain_denial(answer):
    """A bare 'no' — every word inside the approval vocabulary — versus a
    denial that carries content ('no, use port 5433'). Content is worth
    forwarding to the model as feedback instead of discarding; a bare no
    just means stop. Empty or unintelligible-silence answers count as
    plain: there is nothing to forward."""
    words = re.findall(r"[a-z']+", answer.lower())
    return all(w in _NO_WORDS or w in _YES_WORDS or w in _FILLER_WORDS
               for w in words)


_TASK_GRANT_WORDS = {"task", "everything", "all"}


def grants_whole_task(answer):
    """'yes for the whole task' / 'yes to all' — only consulted after
    is_affirmative already said yes. Whole words only, so 'actually'
    doesn't smuggle in 'all'."""
    words = set(re.findall(r"[a-z']+", answer.lower()))
    return bool(words & _TASK_GRANT_WORDS)


# ---------- Sync helpers (recording + transcription) ----------

class Recorder:
    """Keeps the mic stream open for the whole session. Opening the device
    only after the key is pressed loses its startup time — the first syllable
    gets clipped and transcription suffers. A short pre-roll buffer also
    catches speech that starts a beat before the key registers."""

    PREROLL_SECONDS = 0.3

    def __init__(self):
        self._lock = threading.Lock()
        self._preroll = []   # (timestamp, block) pairs kept while idle
        self._frames = None  # active recording, or None when idle
        self.stream = sd.InputStream(
            samplerate=SAMPLERATE, channels=1, callback=self._callback
        )
        self.stream.start()

    def _callback(self, indata, frames_count, time_info, status):
        now = time.time()
        block = indata.copy()
        with self._lock:
            if self._frames is not None:
                self._frames.append(block)
            else:
                self._preroll.append((now, block))
                cutoff = now - self.PREROLL_SECONDS
                while self._preroll and self._preroll[0][0] < cutoff:
                    self._preroll.pop(0)

    def record_while_held(self, prompt=None):
        # The bare between-turns idle doubles as the window for the last-
        # reply fold-out; approval prompts (prompt=...) don't offer it.
        main_idle = prompt is None
        if prompt is None:
            prompt = f"hold {PTT_LABEL} to talk"
        status(dim(f"» {prompt}"))
        if main_idle:
            last_turn_view.drain()
        while not ptt_pressed():
            if main_idle:
                last_turn_view.poll(prompt)
            time.sleep(0.01)
        if main_idle:
            last_turn_view.leave_idle()

        status(f"{red(DOT)} listening — release when done")
        with self._lock:
            self._frames = [block for _, block in self._preroll]
            self._preroll = []

        while ptt_pressed():
            time.sleep(0.01)

        status(dim("transcribing..."))
        with self._lock:
            frames, self._frames = self._frames, None

        if not frames:
            return None
        return np.concatenate(frames, axis=0)


class WhisperSTT:
    def __init__(self, model_name):
        from faster_whisper import WhisperModel  # deferred: see _load_sdk note
        self.model = WhisperModel(
            model_name, device="cpu", compute_type="int8", cpu_threads=4
        )

    def transcribe(self, audio):
        audio_flat = audio.flatten().astype(np.float32)
        segments, info = self.model.transcribe(
            audio_flat, beam_size=1, language="en", vad_filter=True,
            # Domain hint: biases decoding toward developer vocabulary
            initial_prompt="A developer asks a voice assistant about their codebase.",
        )
        return " ".join(segment.text.strip() for segment in segments)


class ParakeetSTT:
    """nvidia parakeet-tdt-0.6b-v2 via onnx-asr: benchmarked on this machine
    at ~2x whisper-small.en speed AND better accuracy (it nearly spelled
    'Mabara' from an old test clip whisper got wrong). int8 despite the
    no-VNNI penalty — the fp32 model is a 2.4 GB download/footprint."""

    def __init__(self):
        import onnx_asr  # deferred: whisper users shouldn't pay the import
        self.model = onnx_asr.load_model(
            "nemo-parakeet-tdt-0.6b-v2", quantization="int8"
        )

    def transcribe(self, audio):
        audio_flat = audio.flatten().astype(np.float32)
        return self.model.recognize(audio_flat, sample_rate=SAMPLERATE).strip()


# ---------- Text cleanup / parsing for TTS ----------

def strip_markdown(text):
    """Remove common markdown so TTS doesn't stumble over symbols."""
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)    # links: keep text, drop URL
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)              # bold
    text = re.sub(r'\*(.*?)\*', r'\1', text)                   # italic
    text = re.sub(r'`(.*?)`', r'\1', text)                     # inline code
    text = re.sub(r'\|.*\|', '', text)                         # table rows
    text = re.sub(r'^-{2,}$', '', text, flags=re.MULTILINE)    # table separators
    text = re.sub(r'#+\s*', '', text)                          # headers
    text = re.sub(r'([.!?:;,])\s*\n+', r'\1 ', text)           # newline already after punctuation
    text = re.sub(r'\n+', '. ', text)                          # remaining line breaks -> pause
    return text.strip()


_PATHLIKE = re.compile(
    r"[A-Za-z]:[\\/][^\s,;:]+"           # windows absolute path
    r"|(?:[\w.\-~]+[\\/]){2,}[\w.\-]+"   # two or more separators
    r"|[\w.\-~]+[\\/][\w\-]+\.\w{1,5}"   # dir/file.ext
)


def speakable(text):
    """Swap path-like tokens for their final component in SPOKEN text only —
    hearing 'C colon backslash Users backslash...' is noise. Exact paths
    stay on screen (tool lines, code blocks, approval prints)."""
    def last_component(match):
        token = match.group(0).rstrip("\\/")
        return re.split(r"[\\/]", token)[-1] or match.group(0)
    return _PATHLIKE.sub(last_component, text)


# ---------- Speaking (background synthesis + gapless playback) ----------

class SupertonicEngine:
    """Supertonic (66M flow matching, ONNX): the naturalness of a modern
    model at 2.1x real-time on this CPU with 8 steps — comfortably above the
    1x knife edge that sank Kokoro. Fewer steps double the speed but audibly
    degrade the M3 voice (M1 tolerates 4 steps if speed is ever needed)."""

    sample_rate = 44100

    def __init__(self, voice_name=SUPERTONIC_VOICE):
        from supertonic import TTS  # deferred: see _load_sdk note
        self.tts = TTS(auto_download=False)
        self.style = self.tts.get_voice_style(voice_name=voice_name)

    def synthesize(self, text):
        wav, _durations = self.tts.synthesize(
            text=text, voice_style=self.style, lang="en",
            total_steps=SUPERTONIC_STEPS, speed=SUPERTONIC_SPEED,
        )
        return np.ascontiguousarray(wav[0], dtype=np.float32)


class PiperEngine:
    """Piper (VITS): a step below Kokoro in naturalness, but ~7x real-time
    on this CPU — speech never falls behind the response."""

    def __init__(self, voice_name=PIPER_DEFAULT_VOICE):
        from piper import PiperVoice, SynthesisConfig  # deferred: see _load_sdk note
        model_path = os.path.join(MODELS_DIR, f"{voice_name}.onnx")
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Piper voice '{voice_name}' not found. Download it with:\n"
                f"  python -m piper.download_voices {voice_name} "
                f"--download-dir \"{MODELS_DIR}\""
            )
        self.voice = PiperVoice.load(model_path)
        self.sample_rate = self.voice.config.sample_rate
        self.syn_config = SynthesisConfig(length_scale=PIPER_LENGTH_SCALE)

    def synthesize(self, text):
        chunks = [
            c.audio_int16_array
            for c in self.voice.synthesize(text, syn_config=self.syn_config)
        ]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32) / 32768.0


class KokoroEngine:
    """Kokoro via ONNX with misaki phonemization (the G2P it was trained
    with — kokoro-onnx's default espeak path audibly degrades pronunciation).
    The most natural voice, but only ~1x real-time on this CPU, so it can
    fall behind on long responses. Imports are lazy so the default piper
    startup doesn't pay for spacy/misaki."""

    sample_rate = 24000

    def __init__(self):
        from kokoro_onnx import Kokoro
        from misaki import en as misaki_en, espeak as misaki_espeak

        self.tts = Kokoro(KOKORO_MODEL_FILE, KOKORO_VOICES_FILE)
        self.g2p = misaki_en.G2P(
            trf=False, british=False,
            fallback=misaki_espeak.EspeakFallback(british=False),
        )

    def synthesize(self, text):
        phonemes, _tokens = self.g2p(text)
        # trim=False keeps Kokoro's natural leading/trailing silence — it
        # doubles as slack for the next synthesis
        samples, _rate = self.tts.create(
            phonemes, voice=TTS_VOICE, speed=1.0, lang="en-us",
            is_phonemes=True, trim=False,
        )
        return np.ascontiguousarray(samples, dtype=np.float32)


class Speaker:
    """Background TTS. Callers queue text with say() and return immediately.
    Synthesis and playback run on separate threads: while one sentence is
    playing, the next is already being synthesized, so slow synthesis doesn't
    open gaps between sentences (playback blocks in stream.write, and a shared
    thread would stall synthesis for that whole duration).

    Text is phonemized with misaki (the G2P Kokoro was trained with) and the
    phonemes are fed to the ONNX engine — espeak phonemization, kokoro-onnx's
    default, audibly degrades pronunciation.

    Utterances are tagged with an epoch; interrupt() bumps the epoch, so
    stale audio is dropped and playback stops within one chunk (~0.2s)."""

    _END = object()  # audio-queue marker: one queued utterance finished
    PLAYBACK_CHUNK = 4800  # 0.2s at 24kHz: bounds barge-in latency
    MAX_BATCH_CHARS = 240  # cap merged synthesis so barge-in stays responsive

    def __init__(self, engine):
        self.engine = engine
        self.text_queue = queue.Queue()
        self.audio_queue = queue.Queue()
        self._pending = 0
        self._audio_backlog = 0  # audio arrays synthesized but not yet played out
        self._epoch = 0
        self._cv = threading.Condition()
        self.stream = sd.OutputStream(
            samplerate=engine.sample_rate, channels=1, dtype="float32"
        )
        self.stream.start()
        threading.Thread(target=self._synth_worker, daemon=True).start()
        threading.Thread(target=self._playback_worker, daemon=True).start()

    def say(self, text):
        text = text.strip()
        if not text:
            return
        with self._cv:
            self._pending += 1
            epoch = self._epoch
        self.text_queue.put((epoch, text))

    def wait_until_done(self):
        """Block until everything queued so far has been spoken. Waits in
        short slices: on Windows an open-ended Condition.wait can't be
        interrupted, so Ctrl+C during speech would otherwise hang until
        the speech finished."""
        with self._cv:
            while self._pending:
                self._cv.wait(timeout=0.2)

    def wait_or_interrupt(self):
        """Block until speech finishes, or until the push-to-talk key cuts
        it off. Returns True if the user barged in (the key is still held,
        so a recording can start immediately)."""
        while True:
            with self._cv:
                if not self._pending:
                    return False
            if ptt_pressed():
                self.interrupt()
                return True
            time.sleep(0.02)

    def interrupt(self):
        """Stop speaking now: unqueued text is dropped, in-flight audio is
        discarded, playback halts within one chunk."""
        with self._cv:
            self._epoch += 1
        while True:
            try:
                self.text_queue.get_nowait()
            except queue.Empty:
                break
            with self._cv:
                self._pending -= 1
                if self._pending == 0:
                    self._cv.notify_all()

    def _current_epoch(self):
        with self._cv:
            return self._epoch

    def _synth_worker(self):
        while True:
            epoch, text = self.text_queue.get()
            # Merge whatever is already queued into one synthesis call: each
            # call costs ~0.5s of fixed overhead, and on this CPU a short
            # sentence alone synthesizes slower than it plays back, so
            # per-sentence calls open gaps. Only merge while earlier audio is
            # still playing to cover the longer synthesis — when nothing is
            # playing (start of a response), go solo so first words come fast.
            batched = 1
            while len(text) < self.MAX_BATCH_CHARS and self._has_audio_backlog():
                try:
                    next_epoch, next_text = self.text_queue.get_nowait()
                except queue.Empty:
                    break
                if next_epoch != epoch:
                    self.text_queue.put((next_epoch, next_text))
                    break
                text = f"{text} {next_text}"
                batched += 1
            if epoch == self._current_epoch():
                try:
                    samples = self.engine.synthesize(text)
                    with self._cv:
                        self._audio_backlog += 1
                    self.audio_queue.put((epoch, samples, text))
                except Exception as e:
                    print(f"(TTS error: {e})")
            for _ in range(batched):
                self.audio_queue.put((epoch, self._END, None))

    def _has_audio_backlog(self):
        with self._cv:
            return self._audio_backlog > 0

    def _playback_worker(self):
        while True:
            epoch, audio, text = self.audio_queue.get()
            if audio is self._END:
                # Fires for spoken, discarded, and failed utterances alike,
                # so _pending always returns to zero.
                with self._cv:
                    self._pending -= 1
                    if self._pending == 0:
                        self._cv.notify_all()
                continue
            if text and epoch == self._current_epoch():
                # Live subtitle: what's playing right now, not a transcript
                shown = text if len(text) <= 62 else text[:61] + "…"
                status(f"{SUB_MARK} {dim(shown)}")
            try:
                for i in range(0, len(audio), self.PLAYBACK_CHUNK):
                    if epoch != self._current_epoch():
                        break
                    self.stream.write(audio[i:i + self.PLAYBACK_CHUNK])
            except Exception as e:
                print(f"(audio playback error: {e})")
            finally:
                with self._cv:
                    self._audio_backlog -= 1


# ---------- Permission callback (auto-approve reads, ask before writes) ----------

READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
# NOTE: the CLI runs its own read-only analysis first and auto-approves
# commands it deems safe — including cd+&&-chained compounds — without ever
# consulting can_use_tool (observed live 2026-07-05: `cd repo && git status`
# never reached the callback). This allowlist therefore only governs what
# the CLI would otherwise ask about; it cannot be the *only* line of
# defense for anything (which is why --readonly also disallows Bash at the
# CLI level, below in main()).
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
    main() sets repo_root, nothing passes — the check fails closed."""
    if not path:
        return True
    if repo_root is None:
        return False
    return _path_within(os.path.expanduser(str(path)), repo_root)


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
        if repo_root is None:
            return False
        path = os.path.join(repo_root, path)
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


def _short_path(path):
    parts = re.split(r"[\\/]", str(path))
    return "/".join(parts[-2:]) if len(parts) > 1 else str(path)


def _feed_path(path):
    """_short_path, except a path outside the repo shows in full: rendering
    C:/Users/DELL/Documents/project/index.html as 'project/index.html' once
    disguised an out-of-repo probe as a local read."""
    p = str(path)
    if os.path.isabs(p) and repo_root and not _path_within(p, repo_root):
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
        detail = str(tool_input.get("description", "") or tool_input.get("prompt", ""))
        return f"agent: {detail[:56]}"
    return name.lower()


# A quiet success marker only when the command was slow enough that the
# silence needed explaining; failures always print.
BASH_OK_MARKER_SECS = 2.5

# Denials already print their own line at deny time — voice denials in the
# approval flow, gate denials (read-only, no-git) right in the callback — so
# a second 'failed' marker for the same event would read as two failures.
# These match the deny messages this file itself sends back to the CLI.
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
    if tool_name in READ_ONLY_TOOLS:
        # Only reachable when the target is outside the repo — in-repo
        # reads were auto-approved before the question was ever asked.
        # The pattern fallback covers Glob asked about an absolute pattern.
        target = (tool_input.get("file_path") or tool_input.get("path")
                  or tool_input.get("pattern") or "an unknown path")
        return f"read {target}, which is outside this repo"
    return f"use the tool {tool_name}"


# Diffs longer than this are truncated on screen — a huge Write must not
# flood a voice-first terminal (the full change still lands in the file).
DIFF_MAX_LINES = 40
# Past this, diffing costs more than the glanceable record is worth.
DIFF_MAX_SOURCE_CHARS = 200_000

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
        print(dim(f"... +{len(lines) - len(shown)} more lines"))
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
        print(dim(f"... +{len(lines) - DIFF_MAX_LINES} more lines"))
    print(dim(rule))


READONLY_DENY = (
    "This session is read-only: edits and commands are disabled. "
    "Explain or show code instead of changing anything."
)
NO_GIT_DENY = (
    "Edits are disabled: this folder is not a git repository, so "
    "there is no safety net to undo changes. Tell the user that "
    "running git init in this folder enables editing."
)


def permission_decision(tool_name, tool_input, *, readonly, task_grants,
                        git_enabled):
    """The policy core of voice_permission_callback, kept free of I/O and
    mutable session state so tests can pin every branch of the most
    security-critical decision in the project. task_grants is the set of
    whole-task grants in force ("edits" or a tool name — see _task_grants).
    Repo confinement still reads module-level repo_root through the
    helpers. Returns one of:
      ("allow", why)    — auto-approve; why in {"read", "bash", "task-grant"}
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

    # --readonly: a hard no for anything that changes state, regardless of
    # approvals — for exploring repos that must not be touched. Checked
    # before the read-only Bash allowlist so that, as the flag's help
    # promises, no shell command runs at all in a read-only session.
    if readonly and tool_name in ("Edit", "Write", "Bash"):
        return ("deny", READONLY_DENY)

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

    # A grant given on any other tool covers only that tool, by name — a
    # yes-to-all on web searches must not leak into anything mutating.
    if tool_name not in ("Bash", "Edit", "Write") and tool_name in task_grants:
        return ("allow", "task-grant")

    return ("ask", None)


async def voice_permission_callback(tool_name, tool_input, context):
    global _approvals_pending

    # The folder can BECOME a repo mid-session (the no-git deny message
    # itself says git init fixes the block), so refresh git state before
    # the policy reads it — trusting the startup snapshot once sent the
    # agent around the gate via a shell heredoc.
    if (tool_name in ("Edit", "Write") and not readonly_mode
            and not git_safety.enabled and git_safety.recheck()):
        clear_status()
        print(f"  {dim(f'{CHECK} git repository detected — edits enabled')}")

    def decide():
        return permission_decision(
            tool_name, tool_input, readonly=readonly_mode,
            task_grants=_task_grants, git_enabled=git_safety.enabled)

    def allow_by_grant():
        # The diff still prints when nobody is asked: an unseen edit
        # is the fastest way to lose the room. Only mutating tools take
        # a checkpoint — a web search has nothing to revert.
        if tool_name in ("Edit", "Write"):
            created = git_safety.before_mutation(tool_name, tool_input)
            if created:
                clear_status()
                print(f"  {dim(CHECKPOINT_HINT)}")
            diff = render_diff(tool_name, tool_input)
            if diff:
                print_diff(diff, tool_input.get("file_path", "?"))
                print(f"  {dim('(auto-approved — whole-task grant)')}")
        return PermissionResultAllow()

    def deny_by_policy(detail):
        # The denial prints: a blocked edit that leaves no mark looks
        # exactly like a successful one in the tool feed.
        blocked = ("blocked (read-only session)" if detail == READONLY_DENY
                   else "blocked: not a git repository")
        clear_status()
        print(f"  {red('!')} {dim(describe_tool_use(tool_name, tool_input) + ' — ' + blocked)}")
        return PermissionResultDeny(message=detail)

    verdict, detail = decide()
    if verdict == "allow":
        return allow_by_grant() if detail == "task-grant" else PermissionResultAllow()
    if verdict == "deny":
        return deny_by_policy(detail)

    # Needs voice approval. Parallel tool calls re-enter this callback
    # concurrently, so the ask itself is serialized: one question, one mic,
    # one answer at a time. The pending counter (not a boolean — every
    # queued ask holds a reference) pauses the barge-in watcher for the
    # whole queue, so an answer is never mistaken for "cut Claude off".
    # Blocking calls run in threads so the SDK's control protocol stays
    # responsive while we talk and listen.
    _approvals_pending += 1
    _pending_asks[tool_name] = _pending_asks.get(tool_name, 0) + 1
    try:
        async with _approval_lock:
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
                return allow_by_grant() if detail == "task-grant" else PermissionResultAllow()
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
            queued = _pending_asks.get(tool_name, 1) - 1
            if queued and tool_name not in ("Bash", "Edit", "Write"):
                more = ("one more like it is" if queued == 1
                        else f"{queued} more like it are")
                question += (f" And {more} waiting — "
                             "you can say yes to all.")
                print(f"  {dim(f'({queued} more {tool_name} queued — say yes to all to approve together)')}")
            # The approval exchange is the most consequential dialogue in a
            # session — it belongs in the transcript like any other speech.
            spoken_question = speakable(question + " Do you approve?")
            append_transcript("Mabara", spoken_question)
            speaker.say(spoken_question)
            # Holding push-to-talk cuts the question short and answers right
            # away — nobody should sit through speech they've already read.
            await asyncio.to_thread(speaker.wait_or_interrupt)

            audio = await asyncio.to_thread(
                recorder.record_while_held, f"hold {PTT_LABEL} to answer (yes / no)"
            )
            if audio is None:
                clear_status()
                print(f"  {dim('no answer — denied')}\n")
                return PermissionResultDeny(message=(
                    "No answer was captured from the user — the microphone "
                    "heard nothing, so this is not a refusal. If the call "
                    "still matters, tell the user and request it once more."))

            answer = (await asyncio.to_thread(stt.transcribe, audio)).lower()
            clear_status()
            print(f"  {cyan('You »')} {answer.strip()}")
            append_transcript("You", answer.strip())

            if is_affirmative(answer):
                created = (git_safety.before_mutation(tool_name, tool_input)
                           if tool_name in ("Bash", "Edit", "Write") else False)
                # "yes for the whole task" / "yes to all" widens the grant:
                # on an edit, to every remaining edit this task; on any
                # other tool, to that tool's remaining calls this task.
                if grants_whole_task(answer):
                    if tool_name in ("Edit", "Write"):
                        _task_grants.add("edits")
                        scope = "edits"
                    else:
                        _task_grants.add(tool_name)
                        scope = f"{tool_name} calls"
                    print(f"  {green('approved')} {dim(f'— and auto-approving {scope} for the rest of this task')}")
                    confirmation = "Okay — I'll handle the rest of those without asking."
                else:
                    print(f"  {green('approved')}")
                    confirmation = "Okay, doing it now."
                append_transcript("Mabara", confirmation)
                speaker.say(confirmation)
                if created:
                    print(f"  {dim(CHECKPOINT_HINT)}")
                print()
                return PermissionResultAllow()
            elif is_plain_denial(answer):
                print(f"  {dim('denied')}\n")
                append_transcript("Mabara", "Okay, I won't do that.")
                speaker.say("Okay, I won't do that.")
                return PermissionResultDeny(message=(
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
                append_transcript("Mabara", "Okay — one sec, let me address that.")
                speaker.say("Okay — one sec, let me address that.")
                return PermissionResultDeny(message=(
                    f'User declined this call and said: "{spoken}". Treat '
                    "that as feedback: revise the plan or the change "
                    "accordingly, and request approval again once it's "
                    "addressed. Don't repeat the identical request."))
    finally:
        _approvals_pending -= 1
        _pending_asks[tool_name] -= 1
        start_thinking()


# ---------- Async helper (Claude conversation) ----------

def get_message_session_id(message):
    """The session ID isn't exposed on the client object; it arrives in the
    message stream (ResultMessage.session_id, and the init SystemMessage's
    data dict)."""
    session_id = getattr(message, "session_id", None)
    if session_id:
        return session_id
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        return data.get("session_id")
    return None


CODE_OPEN = "[CODE]"
CODE_CLOSE = "[/CODE]"
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


class SentenceStreamer:
    """Accumulates streamed text, hands complete sentences to the speaker as
    they arrive, and diverts [CODE]...[/CODE] blocks to the terminal.

    A tag split across deltas (e.g. a buffer ending in '[CO') is safe: text
    is only spoken up to a sentence boundary, and a tag fragment contains no
    sentence-ending punctuation, so it stays buffered until the rest arrives."""

    def __init__(self, speaker):
        self.speaker = speaker
        self.buffer = ""
        self.in_code = False
        self.code_count = 0
        self.sentence_count = 0
        self._transcript = []

    def feed(self, text):
        self.buffer += text
        self._drain(final=False)

    def flush(self):
        """Speak whatever is buffered even without a trailing sentence break.
        Called when a text block ends: a sentence that closes a block has no
        following whitespace, so it would otherwise sit unspoken until the
        next block (e.g. an acknowledgment before a long tool-use phase)."""
        if not self.in_code:
            self._drain(final=True)

    def finish(self):
        self._drain(final=True)

    def _drain(self, final):
        while True:
            if self.in_code:
                idx = self.buffer.find(CODE_CLOSE)
                if idx == -1:
                    if final and self.buffer.strip():
                        self._show_code(self.buffer)
                        self.buffer = ""
                    return
                self._show_code(self.buffer[:idx])
                self.buffer = self.buffer[idx + len(CODE_CLOSE):]
                self.in_code = False
            else:
                idx = self.buffer.find(CODE_OPEN)
                if idx == -1:
                    self._speak_complete_sentences(final)
                    return
                head = self.buffer[:idx]
                self.buffer = self.buffer[idx + len(CODE_OPEN):]
                for part in SENTENCE_BOUNDARY.split(head):
                    self._say(part)
                self.in_code = True

    def _speak_complete_sentences(self, final):
        parts = SENTENCE_BOUNDARY.split(self.buffer)
        if final:
            complete, self.buffer = parts, ""
        else:
            complete, self.buffer = parts[:-1], parts[-1]
        for part in complete:
            self._say(part)

    def _say(self, sentence):
        sentence = strip_markdown(sentence)
        if sentence:
            # Spoken prose stays off the scrollback — the playback subtitle
            # shows it live and the transcript log keeps it re-readable.
            # Speech gets path-sanitized; the transcript keeps the original.
            self.sentence_count += 1
            self._transcript.append(sentence)
            self.speaker.say(speakable(sentence))

    def _show_code(self, code):
        self.code_count += 1
        self._transcript.append(f"[CODE] {code.strip()} [/CODE]")
        rule = "-" * 46
        clear_status()
        print(f"{dim('--[ code ]' + rule[10:])}")
        print(code.strip())
        print(dim(rule))

    def transcript_text(self):
        return " ".join(self._transcript)

    def spoken_lines(self):
        """The turn's sentences for the on-screen fold-out. Code blocks
        were already printed in full above, so they fold to a marker."""
        return ["(code block — shown above)" if s.startswith("[CODE]") else s
                for s in self._transcript]


def describe_result_error(result_text):
    """Turn a raw CLI error result into one short spoken sentence."""
    text = str(result_text or "").strip()
    if "usage limit" in text.lower() or "rate limit" in text.lower():
        spoken = ("I've hit the Claude usage limit, so I can't respond right "
                  "now. Try again after it resets.")
        # Limit errors often carry the reset time: "...reached|1751628800"
        match = re.search(r"\|(\d{9,})", text)
        if match:
            reset = time.strftime("%I:%M %p", time.localtime(int(match.group(1))))
            spoken = (f"I've hit the Claude usage limit, so I can't respond "
                      f"right now. It should reset around {reset}.")
        return spoken
    return "Something went wrong getting a response. The details are on your screen."


def get_stream_text(message):
    """Extract the text delta from a StreamEvent, or None for other events."""
    event = message.event
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta", {})
    if delta.get("type") != "text_delta":
        return None
    return delta.get("text")


def is_thinking_delta(message):
    """True when the model is streaming extended-thinking tokens — activity
    worth surfacing, even though there's nothing speakable in it yet."""
    event = message.event
    if event.get("type") != "content_block_delta":
        return False
    return event.get("delta", {}).get("type") == "thinking_delta"


# Spoken the instant a query goes out: in a voice interface, silence right
# after you finish speaking reads as "it didn't hear me". Local TTS makes
# this nearly free, and the real reply queues right behind it — so keep
# every entry short enough to be done before the first streamed sentence.
# Was briefly collapsed to one fixed phrase after "Let me look." here
# collided with Claude's own "Let me see..." — but that was patching the
# symptom. The real fix is the system prompt telling Claude to never lead
# with a generic acknowledgment, since this line already covers that beat;
# with collision handled at the source, variety is safe again. "Let me
# look." stays retired from the pool regardless, since it's the one
# phrasing most likely to echo Claude's own narration style.
ACKNOWLEDGMENTS = ["On it.", "Okay.", "Alright.", "One sec.", "Got it."]

# Stall watchdog thresholds. Normal first-token waits and auto-approved
# tool runs sit well under half a minute on this machine — past that,
# "slow" and "stuck" must stop looking identical on the status line.
STALL_WARN_SECS = 30
STALL_HINT_SECS = 90


def _fmt_secs(secs):
    """42s / 2m05s — the shape developers read on CI dashboards."""
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m{secs % 60:02d}s"


async def ask_claude(client, text, speaker, label=None):
    """Send text to Claude. Spoken sentences stream to the TTS queue (the
    playback subtitle shows them live); the scrollback gets only artifacts —
    a task header, tool-action lines with outcome markers, code blocks,
    diffs, approvals. Holding push-to-talk mid-response barges in: speech
    stops and the model stops generating. `label` is the user's own words,
    printed as the task header when tools start running.
    Returns (session_id, barged_in, streamer, tool_calls, result_error)."""
    global _first_token_secs
    _task_grants.clear()  # a whole-task grant never outlives its task
    _first_token_secs = None
    query_started = time.time()
    ack = random.choice(ACKNOWLEDGMENTS)
    append_transcript("Mabara", ack)
    speaker.say(ack)
    await client.query(text)
    streamer = SentenceStreamer(speaker)
    session_id = None
    barged_in = False
    tool_calls = 0
    pending_tools = {}  # tool_use id -> (name, started) for outcome markers
    result_error = None
    last_event = time.time()

    async def watch_for_barge_in():
        nonlocal barged_in
        while True:
            if not _approvals_pending and ptt_pressed():
                barged_in = True
                speaker.interrupt()
                try:
                    await client.interrupt()
                except Exception:
                    pass  # response may already be finishing; nothing to stop
                return
            await asyncio.sleep(0.05)

    async def watch_for_stall():
        """A silent stream and a slow model look identical on a spinner —
        after STALL_WARN_SECS of no messages at all, say so out loud, and
        past STALL_HINT_SECS teach the way out. Warns only, never cancels:
        barge-in is already the user's kill switch."""
        nonlocal last_event
        warned = 0
        while True:
            await asyncio.sleep(1.0)
            if barged_in:
                return
            if _approvals_pending:
                # The stream is waiting on the user's yes/no, not hung
                last_event = time.time()
                continue
            quiet = time.time() - last_event
            if quiet < STALL_WARN_SECS:
                warned = 0  # events resumed; re-arm for a later stall
            elif warned == 0:
                warned = 1
                clear_status()
                print(f"  {dim(f'(nothing from Claude in {int(quiet)}s — still waiting)')}")
                spoken = "Still with you — this is taking longer than usual."
                append_transcript("Mabara", spoken)
                speaker.say(spoken)
            elif warned == 1 and quiet >= STALL_HINT_SECS:
                warned = 2
                clear_status()
                print(f"  {dim(f'(no response for {int(quiet)}s — hold {PTT_LABEL} and speak to cut this off)')}")
                spoken = ("Something may be stuck. Hold right control and "
                          "speak, to cut this off and try again.")
                append_transcript("Mabara", spoken)
                speaker.say(spoken)

    watcher = asyncio.create_task(watch_for_barge_in())
    stall_watcher = asyncio.create_task(watch_for_stall())
    start_thinking()
    try:
        async for message in client.receive_response():
            last_event = time.time()
            session_id = get_message_session_id(message) or session_id
            # In-band failures (usage limit, API errors) don't raise — they
            # arrive as an error-flagged result with no spoken text at all,
            # which would otherwise be pure silence.
            if getattr(message, "is_error", False):
                result_error = getattr(message, "result", None) or "unknown error"
            if barged_in:
                continue  # drain quietly until the stream closes
            # Speak only from raw deltas; complete AssistantMessages repeat
            # the same text and would double-speak it.
            if isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
                if is_thinking_delta(message):
                    show_reasoning()
                if message.event.get("type") == "content_block_stop":
                    streamer.flush()
                chunk = get_stream_text(message)
                if chunk:
                    if _first_token_secs is None:
                        _first_token_secs = time.time() - query_started
                    # Clears the "thinking..." line (restarts after approvals)
                    stop_thinking()
                    streamer.feed(chunk)
            elif hasattr(message, "content") and isinstance(message.content, list):
                # Complete AssistantMessages carry the tool calls — one dim
                # line each is what makes the terminal read like work. Tool
                # results echo back the same way (as user messages), and
                # Bash/Edit/Write outcomes get their honesty marker.
                for block in message.content:
                    if hasattr(block, "name") and hasattr(block, "input"):
                        clear_status()
                        if tool_calls == 0 and label:
                            # First tool of the turn: pin the user's own
                            # words above the feed so every line below has
                            # a "what this was for"
                            shown = label if len(label) <= 64 else label[:63] + "…"
                            print(f"  {accent(DOT)} {dim('task:')} {shown}")
                        print(f"  {dim(f'{TOOL_MARK} {describe_tool_use(block.name, block.input)}')}")
                        tool_calls += 1
                        if getattr(block, "id", None):
                            pending_tools[block.id] = (block.name, time.time())
                    elif hasattr(block, "tool_use_id"):
                        outcome = describe_tool_outcome(block, pending_tools)
                        if outcome:
                            clear_status()
                            print(outcome)
    finally:
        watcher.cancel()
        stall_watcher.cancel()
        stop_thinking()
        _task_grants.clear()
    if not barged_in:
        streamer.finish()
    else:
        print(f"  {dim('(you cut in — go ahead)')}")
    if result_error and not barged_in:
        clear_status()
        shown = str(result_error).strip()
        print(f"\n  {red('!')} {shown if len(shown) <= 300 else shown[:300] + '…'}")
        append_transcript("Error", shown)
        speaker.say(describe_result_error(result_error))
    return session_id, barged_in, streamer, tool_calls, result_error


# ---------- Loading screen ----------

MASCOT = r"""
  __  __     _     ___     _     ___     _
 |  \/  |   /_\   | _ )   /_\   | _ \   /_\
 | |\/| |  / _ \  | _ \  / _ \  |   /  / _ \
 |_|  |_| /_/ \_\ |___/ /_/ \_\ |_|_\ /_/ \_\
"""

TAGLINE = "           Code at the speed of speech"


def animate_banner():
    """Sweep the logo in left-to-right (like a waveform being drawn), then
    type the tagline out as if spoken. ~0.6s total. Falls back to a static
    print when stdout isn't a terminal, or when the window is too narrow
    for the art — wrapped lines would break the cursor-up redraw math."""
    art = MASCOT.strip("\n").split("\n")
    width = max(len(line) for line in art)
    if not _USE_COLOR or shutil.get_terminal_size().columns <= width:
        print(MASCOT)
        print(dim(TAGLINE) + "\n")
        return

    out = sys.stdout
    out.write("\033[?25l")  # hide the cursor; redraws flicker with it visible
    try:
        out.write("\n" * (len(art) + 1))  # blank line + reserved art rows
        prev = 0
        for col in range(2, width + 2, 2):
            out.write(f"\033[{len(art)}F")  # back to the first art row
            for line in art:
                seg = line[prev:col]  # only the newly revealed columns —
                if seg:               # never rewrite cells already on screen
                    if prev:
                        out.write(f"\033[{prev}C")
                    out.write(seg)
                out.write("\n")
            out.flush()
            time.sleep(0.012)
            prev = col

        out.write("\n")
        indent = len(TAGLINE) - len(TAGLINE.lstrip())
        out.write(TAGLINE[:indent])
        for ch in TAGLINE[indent:]:
            out.write(dim(ch))
            out.flush()
            if ch != " ":
                time.sleep(0.012)
        out.write("\n\n")
    finally:
        out.write("\033[?25h")
        out.flush()


# The spinner walks through these in order and holds on the last one —
# looping back to "tuning my ears" on a slow load reads as being stuck.
LOADING_PHASES = [
    "tuning my ears...",
    "warming up my voice...",
    "waking up Claude...",
    "loading the last few neurons...",
    "almost there...",
]

# One tip per launch, picked at random and left on screen. (These used to
# rotate through the spinner at 2.5s each — too fast to actually read, and
# they competed with the progress messages.)
TIPS = [
    f"hold {PTT_LABEL} for your whole sentence — release to send",
    f"I talk too much? hold {PTT_LABEL} to cut me off and take over",
    "I never edit or run anything until you say yes out loud",
    "answer 'yes to all' to approve a task's repeated asks in one go",
    "said yes and regret it? just say 'revert that'",
    "happy with a task? say 'commit this' to make it a git commit",
    "say 'switch to sonnet' for hard tasks, 'switch to haiku' for speed",
    "everything we say lands in transcripts.log",
    "each repo gets its own conversation — resume anytime",
]

# Spoken the moment Mabara is ready: the voice is the product, so the first
# proof it works shouldn't wait for the first task — and if the speakers are
# muted or routed wrong, the user finds out now, not mid-conversation.
GREETINGS = [
    "Ready when you are.",
    "I'm listening.",
    "What are we building today?",
]
GREETING_RESUME = "Welcome back. Where were we?"


class Ticker:
    """Animated one-line status for waits we can't shorten (model loading,
    the Claude CLI handshake, silent thinking/tool phases)."""

    def __init__(self, messages, hold_secs=2.5, elapsed_after=None):
        self.messages = list(messages)
        self._hold_secs = hold_secs
        self._elapsed_after = elapsed_after
        self._started = time.time()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_messages(self, messages):
        """Swap the rotation mid-flight (e.g. 'thinking...' becomes
        'reasoning...' once deltas reveal what the silence actually is).
        A plain reference swap — the render loop rereads it every tick."""
        self.messages = list(messages)

    def _run(self):
        frames = "|/-\\"
        i = 0
        while not self._stop_event.is_set():
            elapsed = time.time() - self._started
            # Advance through the messages, then hold on the last one
            messages = self.messages
            msg = messages[min(int(elapsed / self._hold_secs), len(messages) - 1)]
            # On long waits, an elapsed counter is the difference between
            # "frozen" and "attended": the number moving proves liveness.
            if self._elapsed_after is not None and elapsed >= self._elapsed_after:
                msg = f"{msg} {int(elapsed)}s"
            status(f"{frames[i % 4]} {dim(msg)}")
            i += 1
            time.sleep(0.25)

    def stop(self):
        self._stop_event.set()
        self._thread.join()
        clear_status()


# One shared "thinking..." ticker for silent stretches while Claude works.
# ask_claude starts it; the first streamed words or an approval request
# stop it (whichever comes first).
_thinking = None

# 6s per phase: a normal answer never sees past the first message, and the
# elapsed counter only appears on waits long enough to feel worrying.
THINKING_PHASES = ["thinking...", "still thinking...", "working on it..."]
THINKING_HOLD_SECS = 6.0
THINKING_ELAPSED_AFTER = 15.0


def start_thinking():
    global _thinking
    if _thinking is None:
        _thinking = Ticker(THINKING_PHASES, hold_secs=THINKING_HOLD_SECS,
                           elapsed_after=THINKING_ELAPSED_AFTER)


def show_reasoning():
    """Reasoning deltas are streaming: the wait is the model thinking hard,
    not a hang — name it on the status line instead of a generic spinner."""
    if _thinking is not None:
        _thinking.set_messages(["reasoning..."])


def stop_thinking():
    global _thinking
    if _thinking is not None:
        _thinking.stop()
        _thinking = None


# ---------- Main loop ----------

async def main():
    global stt, speaker, recorder, session_saved, git_safety, \
        readonly_mode, debug_mode, repo_root

    args = parse_args()
    args.model = normalize_model_arg(args.model)
    if args.model not in _MODEL_ALIASES.values() and not args.model.startswith("claude-"):
        # Anything else (e.g. a genuinely different pinned version like
        # 'sonnet4.6') would otherwise sail through the whole STT/TTS/SDK
        # load and only fail once the first query round-trips to the CLI —
        # catch it here instead, before any of that work starts.
        print(f"  {red('!')} Unrecognized --model '{args.model}'. Use a "
              f"bare alias (sonnet, haiku, opus) to get that family's "
              f"current default, or its exact full model id (e.g. "
              f"claude-sonnet-5) to pin a specific version — a version "
              f"number tacked onto the alias name isn't enough to tell "
              f"which model you mean.")
        return
    repo_path = os.path.abspath(args.repo)
    repo_root = repo_path  # confines auto-approved reads (permission callback)

    acquired, other_pid = acquire_repo_lock(repo_path)
    if not acquired:
        print(f"\n  {red('!')} another Mabara session (pid {other_pid}) is "
              f"already running on this repo.")
        print(f"  {dim('Two sessions on one repo fight over the mic, checkpoints, and session state.')}")
        print(f"  {dim('Close the other window first — or if it crashed, delete:')}")
        print(f"  {dim(_repo_lock_file(repo_path))}\n")
        return

    def load_stt():
        _load_audio()  # np is used below (and throughout the engines)
        engine = ParakeetSTT() if args.stt == "parakeet" else WhisperSTT(args.stt)
        # Warm-up: lazily-initialized state (VAD model, decode kernels) would
        # otherwise delay the first real utterance.
        engine.transcribe(np.zeros(SAMPLERATE // 2, dtype=np.float32))
        return engine

    def load_tts():
        _load_audio()  # the engines' synthesize() paths use np
        if args.tts == "supertonic":
            engine = SupertonicEngine()
        elif args.tts == "piper":
            engine = PiperEngine(args.voice)
        else:
            engine = KokoroEngine()
        # Warm-up: the first inference initializes ONNX session state that
        # would otherwise delay the first reply.
        engine.synthesize("Warm up.")
        return engine

    # Everything slow — the models, the agent SDK import, and the git
    # subprocess spawns (which crawl while the loader threads peg the CPU) —
    # loads in background threads while the banner draws and the user
    # reads/answers. Yield once so the threads actually start before the
    # animation's sleeps and the blocking input() freeze the event loop.
    stt_task = asyncio.create_task(asyncio.to_thread(load_stt))
    tts_task = asyncio.create_task(asyncio.to_thread(load_tts))
    sdk_task = asyncio.create_task(asyncio.to_thread(_load_sdk))
    git_task = asyncio.create_task(asyncio.to_thread(GitSafety, repo_path))
    await asyncio.sleep(0)

    # Identity first: the banner is the first thing on screen, then context,
    # then (only if relevant) the one question we have — which doubles as
    # useful reading time while the models load behind it.
    animate_banner()

    readonly_mode = args.readonly
    debug_mode = args.debug

    print(f"  {dim('model')} {args.model}   {dim('repo')} {repo_path}")
    print(f"  {dim('spoken transcript')} {dim(TRANSCRIPT_FILE)}")
    git_safety = await git_task
    if args.readonly:
        print(f"  {yellow('read-only')} {dim('— edits and commands are disabled this session')}")
    elif not git_safety.enabled:
        print(f"  {yellow('!')} {dim('not a git repository — edits disabled, exploring only (git init to enable)')}")
    elif git_safety.dirty:
        print(f"  {dim('heads up: uncommitted changes in this repo — consider committing before edit tasks')}")
    print()

    sessions = load_sessions()
    existing_session = sessions.get(repo_path)
    resume_id = None
    if existing_session:
        if prompt_resume(existing_session):
            resume_id = existing_session
        print()

    # The resume prompt above was the last input(); from here on the
    # terminal may report pane focus without typing into anything.
    terminal_focus.enable()

    print(f"  {dim('tip: ' + random.choice(TIPS))}")
    print()

    ticker = Ticker(LOADING_PHASES)
    try:
        await sdk_task  # the names below don't exist until the import lands
    except BaseException:
        ticker.stop()  # same as below: keep the spinner off the traceback
        raise

    # Subagents are poison for a real-time voice loop: Task is
    # auto-approved by the CLI (bypassing voice approval), runs cold and
    # slow, and background agents finish between turns where nobody is
    # listening ("still waiting on that agent..."). Haiku especially
    # loves delegating trivial lookups to them.
    # NotebookEdit is out entirely: the approval flow can't voice a
    # notebook diff and GitSafety's revert doesn't track it — a tool the
    # spoken UX can't honestly describe doesn't belong in the toolset.
    disallowed = ["Task", "NotebookEdit"]
    if args.readonly:
        # The CLI auto-approves Bash it deems read-only (even compound
        # commands) without consulting can_use_tool, so the callback deny
        # alone cannot keep --readonly's promise that no shell command runs
        # at all. Remove the mutating tools from the toolset outright; the
        # callback deny stays as a second layer.
        disallowed += ["Bash", "Edit", "Write"]
    options = ClaudeAgentOptions(
        cwd=repo_path,
        model=args.model,
        allowed_tools=["Read", "Glob", "Grep"],
        disallowed_tools=disallowed,
        can_use_tool=voice_permission_callback,
        resume=resume_id,
        include_partial_messages=True,
        system_prompt=(
            "You are Mabara, a voice-driven coding agent working directly in "
            "the user's codebase. The user speaks to you and hears your "
            "replies read aloud by a text-to-speech engine — they are "
            "listening, not reading. You can explore the code freely; edits "
            "and commands go through a spoken approval step where the user "
            "answers yes or no out loud.\n\n"
            "How to speak: plain, natural, flowing sentences, like a capable "
            "colleague talking while they work. Always use contractions — "
            "it's, you'll, I've, don't, that's, we're — never the expanded "
            "form. Keep sentences short with one idea each; three ideas "
            "chained into a single sentence are hard to follow by ear. Let "
            "a short sentence follow a longer one so the listener gets a "
            "beat to absorb it. Never use parenthetical asides — if it's "
            "worth saying, weave it into the sentence itself. Skip formal "
            "transitions like 'additionally,' 'furthermore,' or 'it is "
            "worth noting that' — just say 'and,' 'but,' 'so,' or 'also.' "
            "Skip abbreviations like 'e.g.,' 'i.e.,' or 'etc.' — say 'for "
            "example,' 'that is,' or 'and so on.' Never use markdown, "
            "bullet points, headers, bold, or tables — instead of a list, "
            "say 'first... second... and third...'. Answer questions at "
            "whatever length they deserve; narrate work tersely. Refer to "
            "files by their short name out loud ('page.tsx' or 'the chat "
            "route'), never a full path — exact paths belong in [CODE] "
            "tags.\n\n"
            "The ONLY exception is literal code: when exact code, a file "
            "path, or a diff genuinely matters, wrap ONLY that part in [CODE] "
            "and [/CODE] tags — it is shown on the user's screen, not spoken. "
            "Say out loud that you've put it on the screen; never read code "
            "aloud symbol by symbol. Keep [CODE] blocks minimal.\n\n"
            "How to work: the app already speaks a short 'on it' the instant "
            "your turn starts, so never open with a generic acknowledgment "
            "like 'sure' or 'let me check that' — start your first sentence "
            "with the specific thing you're doing or finding. Before any "
            "tool use, say one short sentence about what you're about to "
            "do — never start with silent tool use; the "
            "user is sitting in silence and can't see your tools running. "
            "During multi-step tasks, narrate each significant step in a "
            "sentence ('Found it — the default is wrong in parse_args. "
            "Fixing it now.'). Before requesting an edit or a command, state "
            "the reason in one sentence first, so the yes-or-no approval "
            "question that follows makes sense to someone who can't see the "
            "change. For work spanning several files, say the plan out loud "
            "in a sentence or two before you start, and mention that they "
            "can approve edits one by one or say 'yes for the whole task' "
            "to approve them all at once. The same goes for repeated calls "
            "of one tool, like several web searches: approvals are asked "
            "one at a time, so say up front how many you plan and that "
            "'yes to all' approves the rest in one go. When an approval "
            "comes back denied: if the denial quotes the user's words, "
            "that's feedback — say in one sentence how you're addressing "
            "it, revise, and request approval again; a bare denial means "
            "drop that call and ask what they'd like instead, never "
            "retrying the identical request. Always do the work yourself with "
            "your own tools, in this turn — never launch agents or "
            "background tasks: the user is on a live voice call with you "
            "and anything that finishes 'later' finishes never.\n\n"
            "Accuracy discipline: never state facts about the codebase — "
            "its stack, dependencies, structure, or behavior — from memory, "
            "docs, or notes alone. Documentation describes intentions; the "
            "code is the truth. For a stack or dependency question, read "
            "the actual manifests (package.json, requirements.txt, configs) "
            "before answering, every session. If you haven't verified "
            "something, say so plainly instead of sounding sure.\n\n"
            "After changing code: say plainly what changed and where, put "
            "the key changed lines on screen in [CODE] tags when the exact "
            "code matters, and end with how to verify — offer to run the "
            "tests or the app rather than doing it unasked. If something you "
            "tried didn't work, say so directly and what you're trying "
            "instead. When the user asks you to explain or teach, shift into "
            "full tutor mode: unhurried, thorough, spoken explanation."
            + (f"\n\nYour working directory is {repo_path}. Every relative "
               "path resolves there and Bash commands already run from it — "
               "never prefix commands with cd, and never guess at other "
               "locations. Change files only with the Edit and Write tools, "
               "never via shell redirection or heredocs: shell writes bypass "
               "the diff the user approves and the checkpoint that makes "
               "changes revertable. If a tool refusal contradicts what you "
               "can see in the repo, tell the user instead of working "
               "around it.")
            + (f"\n\nYou start this session as the '{args.model}' model. The "
               "user can switch models by voice at any time: 'switch to "
               "haiku' trades depth for speed and a lighter usage quota, "
               "'switch to sonnet' restores full reasoning. If you are the "
               "fast model and a request clearly needs heavy multi-file "
               "engineering, suggest switching back in one sentence before "
               "starting.")
            + ("\n\nThis session is READ-ONLY: file edits and shell commands "
               "are disabled. Never offer to make changes — explain, review, "
               "and point at exact code instead."
               if args.readonly else "")
        ),
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            stt = await stt_task
            speaker = Speaker(await tts_task)
            recorder = Recorder()
            ticker.stop()
            saved_session_id = None
            pending_note = ""

            print(f"  {green(DOT)} ready {dim(f'· hold {PTT_LABEL} to talk · Ctrl+C to quit')}\n")
            # The first sound of the session: proof the whole voice pipeline
            # is live before the user commits words to it. Interruptible like
            # any other speech — holding push-to-talk cuts straight to talking.
            greeting = GREETING_RESUME if resume_id else random.choice(GREETINGS)
            append_transcript("Mabara", greeting)
            speaker.say(greeting)
            speaker.wait_or_interrupt()
            while True:
                audio = recorder.record_while_held()
                if audio is None:
                    continue
                # A quick tap records only the pre-roll; too short to hold speech
                if len(audio) < int(MIN_SPEECH_SECONDS * SAMPLERATE):
                    clear_status()
                    print(f"  {dim(f'(just a tap — hold {PTT_LABEL} down while you speak)')}")
                    continue

                t_stt = time.time()
                text = stt.transcribe(audio)
                stt_secs = time.time() - t_stt
                clear_status()
                if not text.strip():
                    print(f"  {dim('(heard nothing — try again)')}")
                    continue
                # Blank line gives each exchange its own visual block
                print(f"\n  {cyan('You »')} {text.strip()}")

                append_transcript("You", text.strip())

                # 'Revert that' is handled locally with git — deterministic,
                # instant, and no model in the loop for the undo itself.
                if is_revert_command(text):
                    summary = git_safety.revert()
                    print(f"  {green(CHECK)} {dim(summary)}")
                    append_transcript("Mabara", summary)
                    speaker.say(summary)
                    speaker.wait_until_done()
                    # Keep Claude's picture of the code truthful on the
                    # next turn without burning a turn now
                    pending_note = ("[Note: the user reverted your previous "
                                    "file changes with git; those files are "
                                    "back to their pre-task state.] ")
                    continue

                # 'Commit this' graduates the last task's changes into a real
                # commit — drafted locally, approved by voice.
                if is_commit_command(text):
                    preview = git_safety.commit_preview()
                    if preview is None:
                        note = ("This folder isn't a git repository."
                                if not git_safety.enabled
                                else "There are no task changes to commit.")
                        print(f"  {dim(note)}")
                        speaker.say(note)
                        speaker.wait_until_done()
                        continue
                    paths, subject = preview
                    names = ", ".join(os.path.basename(p) for p in paths[:4])
                    if len(paths) > 4:
                        names += f" and {len(paths) - 4} more"
                    file_word = "file" if len(paths) == 1 else "files"
                    print(f"  {yellow('! commit')} — {len(paths)} {file_word}: {names}")
                    print(f"  {dim('message: ' + subject)}")
                    speaker.say(speakable(
                        f"I'll commit {names} with the message: {subject}. Do you approve?"
                    ))
                    speaker.wait_until_done()
                    audio = recorder.record_while_held(f"hold {PTT_LABEL} to answer (yes / no)")
                    answer = stt.transcribe(audio).lower() if audio is not None else ""
                    clear_status()
                    print(f"  {cyan('You »')} {answer.strip() or '(no answer)'}")
                    if is_affirmative(answer):
                        outcome = git_safety.commit(subject)
                        print(f"  {green(CHECK)} {dim(outcome)}")
                        speaker.say(outcome)
                        pending_note = ("[Note: the user committed your recent "
                                        f"file changes to git as: {subject}.] ")
                    else:
                        print(f"  {dim('not committing')}")
                        speaker.say("Okay, I won't commit.")
                    speaker.wait_until_done()
                    append_transcript("Mabara", f"(commit flow) {subject}")
                    continue

                # 'Switch to sonnet/haiku' swaps the model mid-session —
                # same conversation, no cold start; pay for the big brain
                # only while it's engineering.
                target = model_switch_target(text)
                if target:
                    try:
                        await client.set_model(target)
                        print(f"  {green(CHECK)} {dim('model switched to ' + target)}")
                        speaker.say(f"Okay — {target} is driving now.")
                        pending_note = (f"[Note: the user switched you to the "
                                        f"{target} model just now.] ")
                    except Exception as e:
                        print(f"  {red('!')} couldn't switch model: {e}")
                        speaker.say("Sorry, the model switch didn't work.")
                    speaker.wait_until_done()
                    continue

                git_safety.begin_turn(text)
                turn_started = time.time()
                try:
                    session_id, interrupted, streamer, tool_calls, had_error = \
                        await ask_claude(client, pending_note + text, speaker,
                                         label=text.strip())
                    turn_secs = time.time() - turn_started
                    pending_note = ""
                except (KeyboardInterrupt, asyncio.CancelledError):
                    raise
                except Exception as e:
                    # A failed turn (network blip, CLI hiccup) shouldn't kill
                    # the session — report it and keep listening.
                    stop_thinking()
                    clear_status()
                    print(f"\n  {red('!')} something went wrong: {e}")
                    print(f"  {dim('(try asking again)')}")
                    speaker.say("Sorry, something went wrong. Please try again.")
                    speaker.wait_until_done()
                    continue

                log_line = append_transcript("Mabara", streamer.transcript_text())
                last_turn_view.set_turn(streamer.spoken_lines(), log_line)
                if session_id and session_id != saved_session_id:
                    save_session(repo_path, session_id)
                    saved_session_id = session_id
                    session_saved = True
                # Speech plays out; holding push-to-talk cuts it off to talk again.
                if not interrupted and speaker.wait_or_interrupt():
                    print(f"  {dim('(you cut in — go ahead)')}")
                    interrupted = True
                if not interrupted and not had_error:
                    summary = f"done in {_fmt_secs(turn_secs)}"
                    summary += f" · spoke {streamer.sentence_count} sentence" \
                               + ("s" if streamer.sentence_count != 1 else "")
                    if tool_calls:
                        summary += f" · {tool_calls} tool call" + ("s" if tool_calls != 1 else "")
                    if streamer.sentence_count:
                        summary += " · press t to read them"
                    clear_status()
                    print(f"  {green(CHECK)} {dim(summary)}")
                    # The task's receipt: per-file line counts, diffstat-style
                    for rel, added, removed in git_safety.turn_diffstat():
                        print(f"    {dim(rel + ' |')} {green(f'+{added}')} {red(f'-{removed}')}")
                first_token = (f"{_first_token_secs:.1f}s"
                               if _first_token_secs is not None else "n/a")
                append_transcript("Debug", f"stt={stt_secs:.1f}s first_token={first_token}")
                if debug_mode:
                    print(f"  {dim(f'(debug: transcribe {stt_secs:.1f}s · claude first token {first_token})')}")
    except BaseException:
        # Connect failed or Ctrl+C mid-startup: clear the spinner line so it
        # doesn't mangle the traceback / goodbye message.
        ticker.stop()
        raise


def _farewell_and_exit(signum=None, frame=None):
    """Exit on the FIRST Ctrl+C. Letting asyncio unwind normally waits on
    the SDK subprocess teardown, which used to demand a second Ctrl+C.
    There's nothing to flush: sessions and transcripts are saved per turn."""
    stop_thinking()
    clear_status()
    release_repo_lock()       # os._exit below skips atexit
    terminal_focus.disable()  # ditto — or the shell inherits ESC[I/O spam
    farewell = "\n  Goodbye."
    if session_saved:
        farewell += dim("  (this conversation will resume next launch)")
    print(farewell, flush=True)
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _farewell_and_exit)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Fallback if the interrupt arrives before/around the handler setup
        _farewell_and_exit()