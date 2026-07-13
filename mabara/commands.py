"""Spoken-language parsing: the approval vocabulary and the local voice
commands. Deliberately narrow word-allowlists — questions ABOUT commits,
models, or reverting still go to Claude as conversation."""

import re

# Spoken commands arrive dressed in politeness and hesitation — "please um
# revert all the changes you just made" is the same command as "revert
# that". Leading filler is stripped before the first-word check; missing
# this once sent a revert request to the model, which improvised
# `git restore .` — a repo-wide sledgehammer instead of the checkpoint.
_LEADING_FILLER = {
    "please", "okay", "ok", "so", "um", "uh", "hey", "now", "alright",
    "yeah", "mabara", "can", "could", "you", "just",
}


def _command_words(text):
    words = re.findall(r"[a-z']+", text.lower())
    i = 0
    while i < len(words) and words[i] in _LEADING_FILLER:
        i += 1
    return words[i:]


_COMMIT_WORDS = {
    "commit", "that", "this", "it", "them", "these", "the", "change",
    "changes", "task", "tasks", "work", "edit", "edits", "please", "now",
}


def is_commit_command(text):
    """True only for short, unambiguous commands like 'commit this' —
    questions about commits go to Claude as normal conversation."""
    words = _command_words(text)
    return (bool(words) and words[0] == "commit"
            and len(words) <= 8 and all(w in _COMMIT_WORDS for w in words))


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
    words = _command_words(text)
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
    "please", "now", "you", "just", "made", "um", "uh",
}


def is_revert_command(text):
    """True only for short, unambiguous commands like 'revert that' or
    'please revert all the changes you just made' — anything wordier or
    more specific goes to Claude as usual."""
    words = _command_words(text)
    return (bool(words) and words[0] in ("revert", "undo")
            and len(words) <= 8 and all(w in _REVERT_WORDS for w in words))


_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "go", "ahead",
    "approve", "approved", "affirmative", "do", "alright", "fine",
    "absolutely", "definitely", "course",
    # Natural after a plan revision round: "continue" was said as a plain
    # approval and fell to the feedback path (live 2026-07-13)
    "continue", "proceed",
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


# An approval answer that opens like a question is the user talking TO
# Mabara, not answering the ask — "did you hand over to the worker?" once
# became denial-feedback that made the worker rewrite the same file three
# times (live 2026-07-13). Question-shaped answers get an answer-first
# denial: address the human, then re-request the same call unchanged.
_QUESTION_STARTERS = {
    "what", "why", "who", "whose", "which", "how", "where", "when",
    "did", "do", "does", "is", "are", "was", "were", "will", "would",
    "can", "could", "should", "have", "has", "am",
}


def is_question(answer):
    """True when a spoken approval answer opens like a question (leading
    filler skipped). STT gives no punctuation — the first content word is
    the honest signal."""
    words = _command_words(answer)
    return bool(words) and words[0] in _QUESTION_STARTERS


_TASK_GRANT_WORDS = {"task", "everything", "all"}


def grants_whole_task(answer):
    """'yes for the whole task' / 'yes to all' — only consulted after
    is_affirmative already said yes. Whole words only, so 'actually'
    doesn't smuggle in 'all'."""
    words = set(re.findall(r"[a-z']+", answer.lower()))
    return bool(words & _TASK_GRANT_WORDS)
