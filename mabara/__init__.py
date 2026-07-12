"""Mabara — push-to-talk voice coding agent.

The application package. `voice_agent.py` at the repo root is the entry
point; each module here is one section of what used to be a single file,
split along its section-header seams (2026-07-12, roadmap item 0):

    config      paths, tunable constants, ANSI/color bootstrap
    state       mutable runtime state shared across modules (one owner)
    session     push-to-talk key, pane focus arbitration, per-repo lock
    terminal    styling, status line, banner, tickers, last-reply fold-out
    text        TTS text cleanup (markdown stripping, speakable paths)
    commands    spoken approval vocabulary + local voice-command matchers
    transcript  transcript log + per-repo session persistence
    policy      the pure permission policy (unit-tested security core)
    gitsafety   checkpoints, revert, commit
    audio       mic recorder, STT engines, TTS engines, speaker
    approvals   voice approval flow, diffs, spills, side-by-side review
    turn        one Claude turn: streaming, sentence speech, watchdogs
"""
