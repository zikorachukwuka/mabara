"""Mabara's skills: canonical copies live in the repo's skills/ folder,
and get synced to the user's ~/.claude/skills at startup so the CLI can
discover them (setting_sources is pinned to "user" — the one settings
source Mabara trusts, and deliberately the only place skills load from:
a target repo can never inject one).

The mabara- name prefix keeps them recognizable in the user's regular
Claude Code skill listing, which shares the same directory.
"""

import os
import shutil

from . import config

# The repo's canonical skill sources (committed, reviewed like code)
SKILLS_SOURCE_DIR = os.path.join(config._HERE, "skills")
# Where the CLI discovers user-level skills
USER_SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "skills")

# Enabled for every session, in this order. Adding a skill = add its
# folder under skills/ and list it here.
SKILL_NAMES = ["mabara-testing", "mabara-teach", "mabara-recap"]


def sync_skills(source_dir=None, dest_dir=None):
    """Copy each skill's SKILL.md to the user dir when missing or
    changed. Returns the list of installed/updated names; fails soft —
    a sync problem must never block startup (the session just runs
    without that skill)."""
    source_dir = source_dir or SKILLS_SOURCE_DIR
    dest_dir = dest_dir or USER_SKILLS_DIR
    updated = []
    for name in SKILL_NAMES:
        src = os.path.join(source_dir, name, "SKILL.md")
        dst = os.path.join(dest_dir, name, "SKILL.md")
        try:
            with open(src, encoding="utf-8") as f:
                wanted = f.read()
        except OSError:
            continue  # source missing: ship without it
        try:
            with open(dst, encoding="utf-8") as f:
                if f.read() == wanted:
                    continue  # already current
        except OSError:
            pass  # not installed yet
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            updated.append(name)
        except OSError:
            continue
    return updated
