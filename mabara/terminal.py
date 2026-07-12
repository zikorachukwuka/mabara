"""Terminal styling, the in-place status line, banner, tickers, and the
last-reply fold-out. Everything about how Mabara looks on screen."""

import shutil
import sys
import textwrap
import threading
import time

from . import config
from .config import _USE_COLOR, PTT_LABEL
from .session import terminal_focus


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
        ref = config.TRANSCRIPT_FILE + (f":{self._log_line}" if self._log_line else "")
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
