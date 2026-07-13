---
name: mabara-recap
description: Wrap up a session — spoken summary, notes updated, loose ends named. Load when the user says to recap, wrap up, or is ending the session.
---

# Wrapping up a session

A good wrap-up leaves two artifacts: a spoken summary the user can
absorb in under a minute, and updated notes so the next session starts
warm. Facts come from git and the transcript of this session — never
from memory of what you intended to do.

## The procedure

1. **Gather the facts.** Check git status and the session's actual
   changes (diffstat if useful). What was created, changed, decided,
   reverted? What did tests last say?
2. **Speak the recap** — compact, three parts:
   - Done: what changed and where, one sentence per item.
   - Decided: choices made and why, if any mattered.
   - Open: what's unfinished, broken, or waiting on the user, stated
     plainly — an honest loose end beats a tidy lie.
3. **Update the notes** (update_notes tool, full rewrite): fold in the
   durable facts from this session — verified architecture learnings,
   conventions, preferences the user expressed, and exactly where work
   left off so the next session can resume mid-stride. Drop anything
   now stale. Keep it under 120 lines.
4. **Offer the endings, don't perform them:** if there's uncommitted
   task work, offer "commit this"; if something felt wrong, remind
   them "revert that" still works. One sentence each, their call.

## Tone

This is a debrief between colleagues, not a report. Short, concrete,
zero filler — and if the session went sideways somewhere, name it
without ceremony; the notes should record what actually happened.
