"""Getting a Skill folder onto disk: a zip, a folder, a markdown file, a repo.

**A zip is unpacked with the standard library.** The original shelled out to
PowerShell for this, which broke outright on any install path containing a space
— a real bug on the owner's own machine. `zipfile` removes that whole class of
failure, and lets the traversal guard be a real check rather than a hope: every
entry's resolved destination is confirmed to be inside the target directory
before anything is written, so a `../../.env` entry cannot escape.

**Never downloaded code that runs.** An installed Skill is instructions and
supporting files. A helper script inside one is a file like any other until the
user explicitly agrees to let it run, and a pipeline that ARRIVED from elsewhere
starts unapproved.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from ..jscompat import now_iso
from .files import (
    MAX_FILE_BYTES, get_skill, is_skill_folder, parse_skill_md, serialize_skill_md,
    skills_dir, slugify, unique_name, update_skill_state, validate_description,
    validate_name,
)

#: How many pointless wrapper folders to descend through before giving up. A
#: zip made by right-clicking a folder has one; GitHub's zipball always has one.
MAX_WRAPPER_DEPTH = 3
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 2000

GITHUB_REPO = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
    r"(?:\.git)?/?(?:[?#].*)?$")


class InstallError(ValueError):
    """Something about the source is wrong. The message is for the user."""


def _find_skill_root(start: Path) -> Path:
    """The directory that actually holds SKILL.md, descending through wrappers."""
    directory = start
    for _ in range(MAX_WRAPPER_DEPTH):
        if (directory / "SKILL.md").is_file() or (directory / "skill.toml").is_file():
            return directory
        entries = list(directory.iterdir())
        folders = [e for e in entries if e.is_dir()]
        files = [e for e in entries if not e.is_dir()]
        if len(folders) != 1 or files:
            # Ambiguous, or nothing left to descend into. Stop, and let the
            # caller report "no SKILL.md" rather than guessing which way to go.
            return directory
        directory = folders[0]
    return directory


def _unpack(data: bytes, destination: Path) -> None:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise InstallError("That archive is too big to install as a skill.")
    import io

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise InstallError(f"Could not open that zip: {err}") from err

    with archive:
        names = archive.namelist()
        if len(names) > MAX_ENTRIES:
            raise InstallError("That archive has far too many files in it to be a skill.")
        root = destination.resolve()
        for info in archive.infolist():
            # The entry name comes from whoever built the archive and is
            # untrusted: `../../.env` is a real shape, and only resolving the
            # destination catches it.
            target = (destination / info.filename).resolve()
            if target != root and root not in target.parents:
                raise InstallError("That archive tries to write outside its own folder, so "
                                   "I haven't installed it.")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size > MAX_FILE_BYTES * 20:
                raise InstallError(f'"{info.filename}" is too large to install as part of '
                                   f"a skill.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def _describe_folder(directory: Path) -> tuple[str, str]:
    """(declared name, description) from a folder's own SKILL.md or skill.toml."""
    md = directory / "SKILL.md"
    if md.is_file():
        frontmatter = parse_skill_md(md.read_text(encoding="utf-8",
                                                  errors="replace"))["frontmatter"]
        return (str(frontmatter.get("name") or directory.name),
                str(frontmatter.get("description") or ""))

    toml = directory / "skill.toml"
    if toml.is_file():
        from .pipelines import parse

        try:
            return directory.name, parse(toml.read_text(encoding="utf-8")).description or ""
        except ValueError:
            return directory.name, ""
    return directory.name, ""


def install_from_directory(directory: Path | str, *, reserved: set[str] | None = None,
                           source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy a prepared folder in as a new Skill.

    Requires a SKILL.md or a skill.toml AT THE ROOT — the same restriction the
    upload path has always had. A folder with neither is not a Skill, and
    guessing which of its files was meant to be the instructions is exactly the
    kind of helpfulness that installs the wrong thing.
    """
    root = Path(directory)
    if not (root / "SKILL.md").is_file() and not (root / "skill.toml").is_file():
        raise InstallError("That doesn't look like a skill folder — it needs a SKILL.md "
                           "(or a skill.toml) at the top level.")

    declared, description = _describe_folder(root)
    folder = validate_name(unique_name(declared, reserved))
    target = skills_dir() / folder
    shutil.copytree(root, target)

    md = target / "SKILL.md"
    if md.is_file():
        # The folder name is authoritative — see files.list_user_skills. Rewrite
        # the declared name to match so the two can never disagree.
        parsed = parse_skill_md(md.read_text(encoding="utf-8", errors="replace"))
        extra = {k: v for k, v in parsed["frontmatter"].items()
                 if k not in ("name", "description")}
        md.write_text(serialize_skill_md(name=folder,
                                         description=description or f"The {folder} skill.",
                                         body=parsed["body"], extra=extra), encoding="utf-8")

    update_skill_state(folder, {
        "enabled": True, "installedAt": now_iso(),
        "source": source or {"type": "import"},
        # It arrived from outside, so neither its scripts nor its pipeline may
        # run until the user says so.
        "scriptsApproved": False, "pipelineApproved": False})
    return get_skill(folder)  # type: ignore[return-value]


def install_from_zip(data: bytes, *, reserved: set[str] | None = None,
                     source: dict[str, Any] | None = None) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-skill-"))
    try:
        _unpack(data, scratch)
        return install_from_directory(_find_skill_root(scratch), reserved=reserved,
                                      source=source or {"type": "zip"})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def install_from_markdown(text: str, *, reserved: set[str] | None = None,
                          source: dict[str, Any] | None = None) -> dict[str, Any]:
    """A single SKILL.md, pasted or uploaded on its own."""
    parsed = parse_skill_md(text)
    frontmatter = parsed["frontmatter"]
    description = validate_description(frontmatter.get("description")
                                       or "An imported skill.")
    folder = validate_name(unique_name(str(frontmatter.get("name") or "skill"), reserved))
    root = skills_dir() / folder
    root.mkdir(parents=True, exist_ok=False)
    extra = {k: v for k, v in frontmatter.items() if k not in ("name", "description")}
    (root / "SKILL.md").write_text(
        serialize_skill_md(name=folder, description=description, body=parsed["body"],
                           extra=extra), encoding="utf-8")
    update_skill_state(folder, {"enabled": True, "installedAt": now_iso(),
                                "source": source or {"type": "markdown"},
                                "scriptsApproved": False, "pipelineApproved": False})
    return get_skill(folder)  # type: ignore[return-value]


def replace_contents(name: str, directory: Path | str) -> dict[str, Any]:
    """Swap a Skill's files for a newer copy, keeping its name and its state.

    Consent is deliberately reset: the files the user agreed to let run are not
    these files, and carrying the approval across would make "replace" a way to
    get new code past a gate that was answered about something else.
    """
    if not is_skill_folder(name):
        raise InstallError("That skill no longer exists.")
    root = Path(directory)
    if not (root / "SKILL.md").is_file() and not (root / "skill.toml").is_file():
        raise InstallError("That replacement doesn't have a SKILL.md (or a skill.toml) at "
                           "the top level.")

    target = skills_dir() / name
    shutil.rmtree(target)
    shutil.copytree(root, target)
    update_skill_state(name, {"scriptsApproved": False, "pipelineApproved": False,
                              "installedAt": now_iso()})
    return get_skill(name)  # type: ignore[return-value]


def replace_from_zip(name: str, data: bytes) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-skill-"))
    try:
        _unpack(data, scratch)
        return replace_contents(name, _find_skill_root(scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def parse_github_repo(link: str) -> tuple[str, str]:
    match = GITHUB_REPO.match(str(link or "").strip())
    if not match:
        raise InstallError("That doesn't look like a GitHub repository link.")
    return match.group(1), match.group(2)


def install_from_github(link: str, *, reserved: set[str] | None = None,
                        fetch: Any = None) -> dict[str, Any]:
    """Install straight from a public repository.

    Resolves the repo's real default branch first — assuming `main` installs
    nothing from a repo still on `master`, with an error that blames the link.
    The zipball then goes through exactly the same path a local upload does;
    GitHub wraps its contents in one folder, which the wrapper-unwrapping above
    already handles.
    """
    owner, repo = parse_github_repo(link)

    if fetch is None:
        import httpx

        def fetch(url: str) -> Any:  # noqa: ANN401
            with httpx.Client(timeout=30.0, follow_redirects=True,
                              headers={"User-Agent": "Jarvis"}) as client:
                response = client.get(url)
                response.raise_for_status()
                return response

    try:
        described = fetch(f"https://api.github.com/repos/{owner}/{repo}")
        branch = described.json().get("default_branch") or "main"
        zipball = fetch(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}")
        data = zipball.content
    except InstallError:
        raise
    except Exception as err:  # noqa: BLE001
        raise InstallError(f"Couldn't download that repository: {err}") from err

    return install_from_zip(data, reserved=reserved,
                            source={"type": "github", "repo": f"{owner}/{repo}",
                                    "branch": branch})


def export_zip(name: str) -> bytes:
    """A Skill folder as a zip, for the user to keep or share."""
    import io

    root = skills_dir() / validate_name(name)
    if not root.is_dir():
        raise InstallError("That skill no longer exists.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                # Built by hand as a forward-slash string, never from a path
                # join: a backslash here is silently invalid in a zip entry.
                archive.writestr(f"{name}/{path.relative_to(root).as_posix()}",
                                 path.read_bytes())
    return buffer.getvalue()
