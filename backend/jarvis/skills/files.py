"""A Skill folder on disk, and the small runtime state beside it.

`data/skills/<name>/` holds the content; `data/skills.json` holds only per-Skill
runtime state (enabled, consent flags, where it came from) keyed by folder name.
Never skill content — that lives entirely as files, so a folder can be copied in
or out and still be the whole Skill.

A leaf: the store, the clock and the filesystem. Tools import this, and the tool
loader imports every tool, so nothing here may lead back to the loader.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..jscompat import now_iso
from ..store import data_dir, read_json, write_json

STATE_FILE = "skills"
#: Matches the files connector's own read ceiling, for the same reason: a file
#: this big is not something to paste into a conversation.
MAX_FILE_BYTES = 512 * 1024
MAX_DESCRIPTION = 1024

#: Anthropic's own rule for a Skill name, applied after slugifying rather than
#: merely making the string filesystem-safe.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
RESERVED_WORDS = ("anthropic", "claude")


def skills_dir() -> Path:
    target = data_dir() / "skills"
    target.mkdir(parents=True, exist_ok=True)
    return target


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if slug and not slug[0].isalpha():
        slug = f"s-{slug}"
    return slug[:64].rstrip("-") or "skill"


def validate_name(name: str) -> str:
    value = str(name or "")
    if not NAME_RE.match(value):
        raise ValueError("A skill name must be lower-case letters, numbers and hyphens, "
                         "starting with a letter.")
    if any(word in value for word in RESERVED_WORDS):
        raise ValueError('A skill name cannot contain "anthropic" or "claude".')
    return value


def validate_description(description: str) -> str:
    value = str(description or "").strip()
    if not value:
        raise ValueError("A skill needs a description — it is how the model knows when to "
                         "use it.")
    if len(value) > MAX_DESCRIPTION:
        raise ValueError(f"A description has to be under {MAX_DESCRIPTION} characters.")
    return value


def is_skill_folder(name: str) -> bool:
    try:
        validate_name(name)
    except ValueError:
        return False
    return (skills_dir() / name).is_dir()


def skill_root(name: str) -> Path:
    return skills_dir() / validate_name(name)


def resolve_skill_path(name: str, rel_path: str) -> Path:
    """A path inside a Skill's folder, refusing anything that escapes it.

    Checked by comparing the RESOLVED path, never by inspecting the string: a
    `..` segment, an absolute path and a symlink pointing outside all have to
    fail, and only resolution catches the third.
    """
    root = skill_root(name).resolve()
    resolved = (root / str(rel_path or "")).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f'"{rel_path}" is outside this skill\'s folder.')
    return resolved


# --- frontmatter --------------------------------------------------------------
#
# Deliberately a minimal parser, not a YAML library: the same decision the
# original made, for the same reason, and Python has no YAML in its standard
# library either. It handles exactly what a SKILL.md needs — a `---` fence,
# top-level scalars, inline `[a, b]` lists, indented `- item` lists, and block
# scalars. Anything else is left unparsed rather than raising, because a Skill
# written for another tool may carry keys this never has to act on.

def _strip_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _inline_list(text: str) -> list[str] | None:
    value = text.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return None
    inner = value[1:-1].strip()
    return [_strip_quotes(part) for part in inner.split(",")] if inner else []


def parse_skill_md(text: str) -> dict[str, Any]:
    """`{"frontmatter": {...}, "body": "..."}`.

    With no fence at all the whole text is the body, so a plain
    instructions-only file still works rather than erroring.
    """
    source = str(text or "")
    lines = source.split("\n")
    if not lines or lines[0].strip() != "---":
        return {"frontmatter": {}, "body": source}

    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if end == -1:
        return {"frontmatter": {}, "body": source}

    frontmatter: dict[str, Any] = {}
    pending_list_key: str | None = None
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        item = re.match(r"^\s+-\s?(.*)$", line)
        if item and pending_list_key:
            existing = frontmatter.get(pending_list_key)
            values = existing if isinstance(existing, list) else []
            values.append(_strip_quotes(item.group(1)))
            frontmatter[pending_list_key] = values
            index += 1
            continue

        if line[:1].isspace():
            index += 1
            continue

        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            pending_list_key = None
            index += 1
            continue
        key, rest = match.group(1), match.group(2)

        block = re.match(r"^([>|])[+-]?\s*$", rest)
        if block:
            folded = block.group(1) == ">"
            collected: list[str] = []
            base_indent: int | None = None
            cursor = index + 1
            while cursor < end:
                raw = lines[cursor]
                if not raw.strip():
                    collected.append("")
                    cursor += 1
                    continue
                indent = re.match(r"^(\s+)", raw)
                if not indent:
                    break
                if base_indent is None:
                    base_indent = len(indent.group(1))
                if len(indent.group(1)) < base_indent:
                    break
                collected.append(raw[base_indent:])
                cursor += 1
            while collected and collected[-1] == "":
                collected.pop()
            frontmatter[key] = (" ".join(l if l else "\n" for l in collected)
                                .replace(" \n ", "\n").replace("\n ", "\n")
                                .replace(" \n", "\n").strip()
                                if folded else "\n".join(collected))
            pending_list_key = None
            index = cursor
            continue

        if rest == "":
            pending_list_key = key
            index += 1
            continue
        pending_list_key = None

        inline = _inline_list(rest)
        frontmatter[key] = inline if inline is not None else _strip_quotes(rest)
        index += 1

    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return {"frontmatter": frontmatter, "body": body}


def serialize_skill_md(*, name: str, description: str, body: str,
                       extra: dict[str, Any] | None = None) -> str:
    """The inverse, used whenever Jarvis writes a SKILL.md itself. Keeps the file
    human-editable and compatible with the tools that share this format, and
    preserves any frontmatter key already present."""
    import json

    lines = ["---", f"name: {json.dumps(name)}", f"description: {json.dumps(description)}"]
    for key, value in (extra or {}).items():
        if key in ("name", "description") or value in (None, ""):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(json.dumps(str(v)) for v in value)}]")
        else:
            lines.append(f"{key}: {json.dumps(str(value))}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + str(body or "").lstrip("\n")


# --- runtime state ------------------------------------------------------------

def _state() -> dict[str, Any]:
    data = read_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def state_for(name: str) -> dict[str, Any]:
    entry = _state().get(name)
    return entry if isinstance(entry, dict) else {}


def update_skill_state(name: str, patch: dict[str, Any]) -> dict[str, Any]:
    data = _state()
    entry = {**(data.get(name) or {}), **patch, "updatedAt": now_iso()}
    data[name] = entry
    write_json(STATE_FILE, data)
    return entry


def _forget_state(name: str) -> None:
    data = _state()
    if name in data:
        del data[name]
        write_json(STATE_FILE, data)


# --- reading ------------------------------------------------------------------

def list_skill_files(name: str) -> list[str]:
    """Every supporting file, relative to the folder. SKILL.md itself is not one:
    it is the Skill, not a file the Skill happens to carry."""
    root = skill_root(name)
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "SKILL.md":
            continue
        found.append(relative)
    return found


def list_user_skills() -> list[dict[str, Any]]:
    """Every Skill on disk: frontmatter and runtime state, never the body.

    **The only function a Skills UI or a declaration list may enumerate through.**
    It reads folders directly and structurally cannot return a built-in.
    """
    try:
        names = sorted(p.name for p in skills_dir().iterdir()
                       if p.is_dir() and is_skill_folder(p.name))
    except OSError:
        names = []

    skills = []
    for folder in names:
        directory = skills_dir() / folder
        md_path = directory / "SKILL.md"
        has_md = md_path.is_file()
        frontmatter = parse_skill_md(md_path.read_text(encoding="utf-8"))["frontmatter"] \
            if has_md else {}
        allowed = frontmatter.get("allowed-tools")
        state = state_for(folder)
        skills.append({
            # The folder name is what a model calls AND what the UI shows, so it
            # can never drift from where the Skill actually lives even if a
            # downloaded SKILL.md's own `name:` disagrees.
            "name": folder,
            "declaredName": frontmatter.get("name") or folder,
            "description": frontmatter.get("description") or "",
            "hasSkillMd": has_md,
            "hasToml": (directory / "skill.toml").is_file(),
            "allowedTools": (allowed if isinstance(allowed, list)
                             else [allowed] if isinstance(allowed, str) else []),
            "enabled": state.get("enabled") is not False,   # default on
            "source": state.get("source") or {"type": "user"},
            "installedAt": state.get("installedAt"),
            "updatedAt": state.get("updatedAt"),
            # Whether the user has agreed to let THIS Skill's helper scripts run
            # in the sandbox.
            "scriptsApproved": state.get("scriptsApproved") is True,
            # Whether a skill.toml pipeline here may RUN. Set at creation for a
            # Skill written in-app (you wrote and reviewed it); false for
            # anything that arrived by upload or install until explicitly
            # approved. Unrelated to scriptsApproved: different mechanism,
            # different default.
            "pipelineApproved": state.get("pipelineApproved") is True,
        })
    return skills


def get_skill(name: str) -> dict[str, Any] | None:
    if not is_skill_folder(name):
        return None
    return next((s for s in list_user_skills() if s["name"] == name), None)


def read_skill_body(name: str) -> str:
    """The instructions, read only when a Skill is actually invoked or opened —
    never as part of the per-turn declaration list. Empty rather than an error
    for a pipeline-only Skill with no SKILL.md at all."""
    md_path = skill_root(name) / "SKILL.md"
    if not md_path.is_file():
        return ""
    return parse_skill_md(md_path.read_text(encoding="utf-8"))["body"]


def read_skill_md(name: str) -> dict[str, Any]:
    md_path = skill_root(name) / "SKILL.md"
    if not md_path.is_file():
        return {"raw": "", "frontmatter": {}, "body": ""}
    raw = md_path.read_text(encoding="utf-8")
    return {"raw": raw, **parse_skill_md(raw)}


def read_skill_toml(name: str) -> str | None:
    """Raw text only. Parsing is a separate, pure step, so validation and
    execution never need filesystem access at all."""
    path = skill_root(name) / "skill.toml"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def read_skill_file(name: str, rel_path: str) -> str:
    resolved = resolve_skill_path(name, rel_path)
    if resolved.is_dir():
        raise ValueError(f'"{rel_path}" is a folder, not a file.')
    if not resolved.is_file():
        raise FileNotFoundError(f'"{rel_path}" is not in this skill\'s folder.')
    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f'"{rel_path}" is too large to read this way '
                         f"({round(size / 1024)}KB).")
    return resolved.read_text(encoding="utf-8", errors="replace")


# --- writing ------------------------------------------------------------------

def unique_name(base: str, reserved: set[str] | None = None) -> str:
    """A free name, avoiding both existing folders and anything already taken by
    a built-in or connector tool.

    `reserved` is passed IN: this module never imports the capability registry,
    so it cannot know that list on its own without becoming the cycle the leaf
    rule exists to prevent.
    """
    taken = reserved or set()
    slug = slugify(base)
    candidate, suffix = slug, 2
    while candidate in taken or (skills_dir() / candidate).exists():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def create_skill(*, name: str, description: str, instructions: str,
                 reserved: set[str] | None = None,
                 source: dict[str, Any] | None = None) -> dict[str, Any]:
    if not (name or "").strip():
        raise ValueError("A skill needs a name.")
    described = validate_description(description)
    folder = validate_name(unique_name(name, reserved))

    root = skills_dir() / folder
    root.mkdir(parents=True, exist_ok=False)
    (root / "SKILL.md").write_text(
        serialize_skill_md(name=folder, description=described, body=instructions or ""),
        encoding="utf-8")
    update_skill_state(folder, {"enabled": True, "installedAt": now_iso(),
                                "source": source or {"type": "user"},
                                # Written and reviewed here, so its own pipeline
                                # may run. Anything that ARRIVED from elsewhere
                                # starts unapproved.
                                "pipelineApproved": source is None})
    return get_skill(folder)  # type: ignore[return-value]


def update_skill_md(name: str, *, description: str | None = None,
                    instructions: str | None = None) -> dict[str, Any]:
    current = read_skill_md(name)
    frontmatter = current["frontmatter"]
    described = validate_description(description if description is not None
                                     else frontmatter.get("description", ""))
    body = current["body"] if instructions is None else instructions
    extra = {k: v for k, v in frontmatter.items() if k not in ("name", "description")}
    (skill_root(name) / "SKILL.md").write_text(
        serialize_skill_md(name=name, description=described, body=body, extra=extra),
        encoding="utf-8")
    update_skill_state(name, {})
    return get_skill(name)  # type: ignore[return-value]


def delete_skill(name: str) -> None:
    import shutil

    root = skill_root(name)
    if root.is_dir():
        shutil.rmtree(root)
    _forget_state(name)
