"""Folder Skills: instructions, never a rename of an executable capability.

A Skill is a folder holding a `SKILL.md` — YAML-ish frontmatter plus
instructions — and any supporting files it needs, laid out exactly the way
Claude Code and skills.sh do, so a folder from either drops in unchanged.

**The distinction from a built-in tool is structural, not remembered.**
`list_user_skills()` reads `data/skills/` directly and has no code path that can
return a built-in ability or a connector tool. Any UI that lists "things Jarvis
can do" must be built on a source that structurally cannot return a native
ability — never on a filtered capability list.

**Jarvis never executes a script inside a Skill folder as a matter of course** —
it reads them, like any other file. Running one is `run_skill_script`, behind a
one-time per-Skill consent gate, because unattended runs auto-confirm and "run
it, but ask first" would silently skip the asking there.
"""

from .files import (
    create_skill, delete_skill, get_skill, list_skill_files, list_user_skills,
    parse_skill_md, read_skill_body, read_skill_file, read_skill_md, read_skill_toml,
    serialize_skill_md, skill_root, slugify, update_skill_md, update_skill_state,
    validate_description, validate_name,
)

__all__ = [
    "create_skill", "delete_skill", "get_skill", "list_skill_files", "list_user_skills",
    "parse_skill_md", "read_skill_body", "read_skill_file", "read_skill_md",
    "read_skill_toml", "serialize_skill_md", "skill_root", "slugify", "update_skill_md",
    "update_skill_state", "validate_description", "validate_name",
]
