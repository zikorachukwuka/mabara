"""One Claude turn: query, streaming deltas to speech, tool-feed lines,
barge-in and stall watchdogs."""

import asyncio
import random
import re
import time

from . import state, transcript
from .approvals import describe_tool_outcome, describe_tool_use
from .config import PTT_LABEL
from .session import ptt_pressed
from .terminal import (
    DOT, TOOL_MARK, accent, clear_status, dim, red, show_reasoning,
    start_thinking, status, stop_thinking,
)
from .text import speakable, strip_markdown


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
    state._task_grants.clear()  # a whole-task grant never outlives its task
    state._first_token_secs = None
    query_started = time.time()
    ack = random.choice(ACKNOWLEDGMENTS)
    transcript.append_transcript("Mabara", ack)
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
            if not state._approvals_pending and ptt_pressed():
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
            if state._approvals_pending:
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
                transcript.append_transcript("Mabara", spoken)
                speaker.say(spoken)
            elif warned == 1 and quiet >= STALL_HINT_SECS:
                warned = 2
                clear_status()
                print(f"  {dim(f'(no response for {int(quiet)}s — hold {PTT_LABEL} and speak to cut this off)')}")
                spoken = ("Something may be stuck. Hold right control and "
                          "speak, to cut this off and try again.")
                transcript.append_transcript("Mabara", spoken)
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
            if isinstance(message, state.StreamEvent) and message.parent_tool_use_id is None:
                if is_thinking_delta(message):
                    show_reasoning()
                if message.event.get("type") == "content_block_stop":
                    streamer.flush()
                chunk = get_stream_text(message)
                if chunk:
                    if state._first_token_secs is None:
                        state._first_token_secs = time.time() - query_started
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
        state._task_grants.clear()
    if not barged_in:
        streamer.finish()
    else:
        print(f"  {dim('(you cut in — go ahead)')}")
    if result_error and not barged_in:
        clear_status()
        shown = str(result_error).strip()
        print(f"\n  {red('!')} {shown if len(shown) <= 300 else shown[:300] + '…'}")
        transcript.append_transcript("Error", shown)
        speaker.say(describe_result_error(result_error))
    return session_id, barged_in, streamer, tool_calls, result_error
