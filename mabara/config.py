"""Paths, tunable constants, and console bootstrap.

Imported first by every other module, so the two import-time side effects
below (HF_HUB_OFFLINE and the ANSI escape-mode switch) run before anything
that depends on them.
"""

import os
import sys

# All models are cached locally, so skip HuggingFace Hub's startup network
# checks. If you ever switch to a Whisper model you haven't downloaded yet,
# run once with HF_HUB_OFFLINE=0 in the environment to allow the download.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

os.system("")  # switches Windows consoles into ANSI escape mode
_USE_COLOR = sys.stdout.isatty()

# The repo root (this file lives in mabara/, one level down)
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(_HERE, "models")   # downloaded TTS models/voices
DATA_DIR = os.path.join(_HERE, "data")       # runtime state (sessions, transcripts)
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_STORE_FILE = os.path.join(DATA_DIR, "sessions.json")
TRANSCRIPT_FILE = os.path.join(DATA_DIR, "transcripts.log")

# Rotation cap: transcripts hold everything both sides say, in plaintext,
# forever — bound the exposure (and the disk) at ~2x this across the live
# file and one .1 backup instead of growing without limit.
TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024

SAMPLERATE = 16000
# Recordings shorter than this are a key tap (pre-roll only), not speech
MIN_SPEECH_SECONDS = 0.4
# Right Ctrl instead of space: space made typing impossible while Mabara
# talks (any space bar press triggered barge-in). Right Ctrl is never part
# of normal typing (shortcuts live on left Ctrl) and is comfortable to hold.
PUSH_TO_TALK_KEY = "right ctrl"
PTT_LABEL = "RIGHT CTRL"  # how the key is written in on-screen hints

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
