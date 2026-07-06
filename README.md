# Mabara

**A push-to-talk voice coding agent. Hold a key, talk to your codebase, hear it talk back - and let it edit, with your spoken approval.**

Built over a weekend of relentless benchmarking on a modest laptop (i5-10210U, 8 GB RAM, no GPU), Mabara turns Claude into a hands-free pair programmer: local ears, local mouth, cloud brain. Everything runs real-time on CPU.

🎬 **Watch it in action:**

https://github.com/user-attachments/assets/f56e96d3-158f-4065-9ca9-bbbf9585a301

```
  __  __     _     ___     _     ___     _
 |  \/  |   /_\   | _ )   /_\   | _ \   /_\
 | |\/| |  / _ \  | _ \  / _ \  |   /  / _ \
 |_|  |_| /_/ \_\ |___/ /_/ \_\ |_|_\ /_/ \_\

           Code at the speed of speech
```

## What a session looks like

```
  ● ready · hold RIGHT CTRL to talk · Ctrl+C to quit

  You » what's the tech stack of this project?
  · read frontend/package.json
  · read backend/requirements.txt
  √ spoke 14 sentences · 2 tool calls

  You » add a comment explaining the speculative fetch

  ! approval needed — Mabara wants to edit the file app/page.tsx
  You » yes
  approved
  · edit app/page.tsx
  √ spoke 6 sentences · 3 tool calls

  You » revert that
  √ Done — restored 1 file.

  ♪ …the comment is in — want me to run the build?   ← live subtitle
```

The terminal shows **artifacts, not prose**: your words, tool actions, code blocks, approvals, receipts. What Mabara *says* plays as audio with a one-line live subtitle — the full transcript lands in a log file. Voice is the interface; the screen is the instrument panel.

## Setup

**Requirements:** Windows 10/11, Python 3.11+, a microphone, [Claude Code](https://claude.com/claude-code) installed and logged in (Mabara drives it through the Agent SDK), and git.

```powershell
git clone https://github.com/zikorachukwuka/mabara
cd mabara
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# — or, supply-chain-verified: every package (including transitive deps)
#   pinned and sha256-checked against the versions this repo was built with
venv\Scripts\pip install --require-hashes -r requirements.lock

# Voice model (~60 MB, one time)
venv\Scripts\python -m piper.download_voices en_US-joe-medium --download-dir models

# First run downloads the speech-recognition model (~600 MB) to the HF cache
$env:HF_HUB_OFFLINE = "0"
venv\Scripts\python voice_agent.py --repo path\to\your\project
```

Optional PowerShell profile function, so any project folder is one word away:

```powershell
function mabara {
    & "C:\path\to\mabara\venv\Scripts\python.exe" "C:\path\to\mabara\voice_agent.py" @args
}
```

### Flags

| Flag | Default | Notes |
|---|---|---|
| `--repo` | `.` | codebase to talk about (each repo gets a resumable session) |
| `--model` | `sonnet` | alias or full model id; switchable by voice mid-session |
| `--stt` | `parakeet` | or `small.en` / `base.en` (whisper fallbacks) |
| `--tts` | `piper` | or `supertonic` (more natural, slower start) / `kokoro` |
| `--voice` | `en_US-joe-medium` | piper voice (`hfc_male`, `amy` also good) |
| `--readonly` | off | hard-disables edits and commands |
| `--debug` | off | per-turn latency breakdown |

## Features

- **Push-to-talk** (Right Ctrl) with an always-open mic and pre-roll buffer, so your first syllable is never clipped
- **Streaming speech** - Mabara starts talking after its first *sentence* is ready, not after the full response; sentences batch adaptively when synthesis needs headroom
- **Barge-in** - hold the key while Mabara talks: playback stops within 0.2s, the model stops generating, and it's instantly listening to you
- **Voice-gated tool safety** - reads are free; every edit and shell command is spoken aloud and requires your verbal "yes"; answering *"yes, for the whole task"* auto-approves the rest of that task's edits (shell commands always ask)
- **Git safety net** - edits only allowed inside a git repo; every edit-task gets an automatic checkpoint; **"revert that"** undoes the last task deterministically (including restoring your own untracked files rather than deleting them); **"commit this"** turns a finished task into a real commit - only the task's files, never your unrelated changes
- **Dual-brain economics** - **"switch to haiku"** / **"switch to sonnet"** swaps the model *mid-conversation* with full context retained: quality by default, quota-stretching on demand
- **Per-repo resumable sessions**, spoken error reporting (including usage-limit warnings with reset times), path-sanitized speech (you hear "page.tsx", never "C colon backslash..."), and a `--readonly` look-don't-touch mode

## Voice commands

| Say | Happens |
|---|---|
| *(anything else)* | goes to Claude |
| "yes" / "no" | answer an approval |
| "yes, for the whole task" | approve all remaining edits this task |
| "revert that" | git-restore everything the last task touched |
| "commit this" | commit the last task's files (voice-approved message) |
| "switch to haiku / sonnet / opus" | swap model mid-conversation |

Command matchers are deliberately narrow word-allowlists - "how do I undo a commit?" is conversation, "undo that" is a command.

## Architecture

```
 hold key ──► Recorder (always-open mic + pre-roll)
                 │ release
                 ▼
          Parakeet-TDT 0.6B (ONNX, local) ──► text
                 │
     local intercepts: "revert that" · "commit this" · "switch to <model>"
                 │ otherwise
                 ▼
          Claude Agent SDK (streaming deltas)
                 │                        │
        SentenceStreamer          voice approval callback
        (sentence boundaries,     (Edit/Write/Bash → spoken
         [CODE] blocks → screen)   yes/no + git checkpoint)
                 ▼
          Speaker: synth thread ──► audio queue ──► playback thread
          (Piper TTS, batched)      (epoch-tagged    (gapless stream,
                                     for barge-in)    live subtitle)
```

One Python file, one process, five threads, no framework. That's deliberate: a real-time voice loop is a *system*, and keeping it in one place kept every latency bug findable.

## The decision log (why each part is what it is)

Every component was chosen by benchmark on the target machine — and several "obvious" choices lost. Real numbers, measured 2026-07-04 on an i5-10210U:

### Ears: Whisper → Parakeet

| Model | 9.2s clip | Accuracy on my voice |
|---|---|---|
| whisper small.en (int8, greedy) | 4.9s | good |
| distil-small.en | **no faster** | worse |
| **parakeet-tdt-0.6b-v2 (onnx, int8)** | **2.3s** | **better** (spelled "Mabara" where whisper heard "my bar") |

Distil-Whisper's headline speedups never materialized for push-to-talk: short clips are *encoder*-bound and distillation only shrinks the decoder. Parakeet won on both axes at once.

### Mouth: Kokoro → Piper (with detours)

| Engine | Real-time factor (CPU) | Verdict |
|---|---|---|
| Kokoro (PyTorch) | 1.4× | most natural, can't keep up |
| Kokoro (fp32 ONNX + misaki G2P) | 1.6× | still knife-edge; gaps between sentences |
| Kokoro (**int8** ONNX) | **0.6×** | *slower* than fp32 — this CPU has no VNNI |
| Supertonic M1 (66M flow matching) | 2.1–4.2× | near-Kokoro quality, but ~1.6s to first word |
| **Piper joe-medium** | **~7×** | robotic edge, **0.4s to first word** - wins |

Two findings worth stealing: **int8 quantization is a *slowdown* on CPUs without VNNI** (benchmark before you quantize), and **for a conversational agent, response latency beats voice beauty** - I lived with a robotic voice for a day happily, but 1.2 extra seconds of silence before every reply lasted twenty minutes. Supertonic remains available behind `--tts supertonic`.

### Brain: model economics, measured

Haiku answered ~2s faster and stretches subscription quota - but in live use it delegated trivial questions to background agents (stranding the voice loop) and confidently mis-stated the backend stack from docs without reading the manifests. Both got prompt-level fixes, but the default is **Sonnet: wrong-but-confident is the most expensive output a voice tool can produce.** Haiku stays one spoken sentence away.

### Latency budget (release key → first spoken word)

| Stage | Cost |
|---|---|
| transcription (parakeet) | 0.2–0.8s |
| Claude first token (sonnet) | ~2.5–4s ← the floor |
| first-sentence synthesis (piper) | ~0.3s |

The local pipeline is squeezed to near its physical floor; what remains is the model thinking. Also killed along the way: streaming transcription-while-talking (built for Whisper's 5s latency, removed when Parakeet made it pointless jitter) and DirectML iGPU offload (Kokoro's ConvTranspose crashes DML).

## Development

Unit tests cover the safety-critical parsing — spoken approval answers, the read-only shell allowlist, repo confinement, the permission policy, and the voice-command matchers:

```powershell
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m pytest tests
```

The manual hardware checks (mic, hotkey, STT, SDK connectivity) live in [scripts/](scripts/) — they need a microphone and ears, so they're scripts, not tests.

## Security & privacy

Things you should know before pointing Mabara at code:

- **Approvals fail closed.** Spoken yes/no answers are parsed on whole words with deny-words checked first - "no, that looks broken" is a *no* (the letters "ok" inside a word never count as approval), "yes… wait, no" is a *no*, and silence or ambiguity is a *no*. An approval must also consist *entirely* of approval words: a question that happens to contain one ("okay, show me the diff first", "what will this do") is a *no*, not a yes. The parsing lives in `is_affirmative` and is unit-tested in `tests/`.
- **Auto-approved reads stay inside the repo.** Read/Glob/Grep (and `cat`/`type`) on any path outside the `--repo` folder require a spoken yes — including Glob patterns that are absolute paths — so a prompt-injected "read `~/.ssh/id_rsa`" in someone else's repo can't sail through. The "yes for the whole task" grant is repo-confined the same way: an edit aimed outside the repo always comes back as a spoken question that says so. Still: **be deliberate about running any coding agent against untrusted repositories** - file *contents* inside the repo are read freely and sent to the Claude API. The whole permission policy is one pure function (`permission_decision`), unit-tested in `tests/`.
- **The read-only shell allowlist is narrow.** Only exact whole-word commands (`ls`, `git status`, `git log`, …) with no chaining, redirection, or file-writing flags (`--output`) auto-run; everything else needs your voice. (The Claude CLI additionally runs its own read-only analysis and may auto-run commands it deems safe — like `cd repo && git status` — before Mabara is ever consulted; anything mutating still lands on voice approval.) In `--readonly` mode the mutating tools are removed from the toolset entirely, so no shell command runs at all.
- **Transcripts are plaintext.** Everything both sides say lands in `data/transcripts.log` (rotated at 5 MB, one `.1` backup kept). It's gitignored, but treat it like a private notebook - anything Claude reads aloud, including code, ends up there.
- **The `keyboard` dependency installs a global keyboard hook** (that's how push-to-talk works from any window). It requires root on Linux, and while Mabara only polls one key, you should know the hook sees all keystrokes while the process runs. It is also effectively unmaintained (no release since 2020) — a lot of trust for a package with that much access; the hash-pinned lockfile below keeps it at least byte-identical to what was audited at lock time.
- **Dependencies are hash-pinned.** `requirements.lock` pins every package, including transitive ones, with sha256 hashes — `pip install --require-hashes -r requirements.lock` refuses anything PyPI didn't serve byte-for-byte at lock time.
- **Git is resolved by absolute path at startup** and refused if it lives inside the target repo, so an untrusted repo can't plant its own `git.exe` (Windows searches the current directory for executables).

## Honest limitations

- **Tuned for one machine.** Every default here won a benchmark on *my* laptop. On yours, the losers might win - the flags exist so you can re-run the bake-off. (If your CPU has VNNI, int8 models may actually be fast for you.)
- **Windows-first.** The `keyboard` global hotkey and console handling are Windows-tested; Linux/macOS would need small changes.
- **English-only** speech pipeline (the models have multilingual variants if you're adventurous).
- Claude usage is billed through your Claude Code subscription/API - Mabara reports usage-limit errors out loud rather than pretending they didn't happen.

## Roadmap

Wake-word activation ("Hey Mabara" instead of push-to-talk) and long-task narration polish. The safety net (checkpoints, voice approvals, revert/commit) is done and battle-tested.

## How it was built

Mabara was pair-built over a weekend with Claude Code - the AI wrote most of the lines; every decision was human. Which benchmarks to run, which trade-offs to accept, which defaults to revert (three times, when the measurements disagreed with the hype) - that's the part that can't be delegated, and the decision log above is the receipts.

## License

MIT - see [LICENSE](LICENSE).
