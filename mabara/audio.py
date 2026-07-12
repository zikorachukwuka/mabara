"""The audio stack: mic recorder, STT engines, TTS engines, and the
speaker's synthesis/playback pipeline — the real-time core, kept together
because its latency behavior is a system, not a set of parts."""

import os
import queue
import threading
import time

from . import config, session
from .approvals import poll_review_key
from .config import (
    KOKORO_MODEL_FILE, KOKORO_VOICES_FILE, MIN_SPEECH_SECONDS, MODELS_DIR,
    PIPER_DEFAULT_VOICE, PIPER_LENGTH_SCALE, PTT_LABEL, SAMPLERATE,
    SUPERTONIC_SPEED, SUPERTONIC_STEPS, SUPERTONIC_VOICE, TTS_VOICE,
)
from .session import ptt_pressed
from .terminal import (
    DOT, SUB_MARK, dim, last_turn_view, red, status,
)

# sounddevice + numpy + keyboard cost ~1.1s to import — part of the
# blank-terminal time before the banner's first frame. main() loads them in
# background threads (alongside the STT/TTS models) while the banner draws;
# nothing below touches these names until _load_audio has finished.
# faster-whisper and piper are likewise imported inside the engine classes
# that use them, off the main thread.
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
            session.attach_keyboard(_keyboard)


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

    def record_while_held(self, prompt=None, review=None):
        # The bare between-turns idle doubles as the window for the last-
        # reply fold-out. Approval waits offer D = side-by-side instead,
        # when the caller passes the pending (tool_name, tool_input).
        main_idle = prompt is None
        if prompt is None:
            prompt = f"hold {PTT_LABEL} to talk"
        status(dim(f"» {prompt}"))
        if main_idle:
            last_turn_view.drain()
        elif review is not None:
            session.terminal_focus.discard_keys()
        while not ptt_pressed():
            if main_idle:
                last_turn_view.poll(prompt)
            elif review is not None:
                poll_review_key(review, prompt)
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
        from faster_whisper import WhisperModel  # deferred: see _load_audio note
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


# ---------- Speaking (background synthesis + gapless playback) ----------

class SupertonicEngine:
    """Supertonic (66M flow matching, ONNX): the naturalness of a modern
    model at 2.1x real-time on this CPU with 8 steps — comfortably above the
    1x knife edge that sank Kokoro. Fewer steps double the speed but audibly
    degrade the M3 voice (M1 tolerates 4 steps if speed is ever needed)."""

    sample_rate = 44100

    def __init__(self, voice_name=SUPERTONIC_VOICE):
        from supertonic import TTS  # deferred: see _load_audio note
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
        from piper import PiperVoice, SynthesisConfig  # deferred: see _load_audio note
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
