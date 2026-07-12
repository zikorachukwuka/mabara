"""Text cleanup for speech: what leaves the TTS engine's mouth."""

import re


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
