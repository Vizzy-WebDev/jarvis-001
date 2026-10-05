"""The repository layout: one `jarvis` package kept in several folders, asserted rather than trusted.

`server/jarvis/__init__.py` adds each group folder at the repository root to the package's own
search path. Python then takes the FIRST folder holding a name and never looks further, so a
second `jobs/` or `durable.py` in another group would be ignored silently — the code that runs
would not be the code someone just edited. These tests make that a failure instead.
"""

from __future__ import annotations

import importlib
from collections import defaultdict
from pathlib import Path

import jarvis


def _names_in(folder: Path) -> set[str]:
    """What a folder contributes to the package: its modules and its packages."""
    names = set()
    for entry in folder.iterdir():
        if entry.suffix == ".py":
            names.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
    return names


def test_every_group_folder_is_on_the_package_path():
    roots = [Path(entry).resolve() for entry in jarvis.__path__]
    expected = [jarvis.REPO_ROOT / "server" / "jarvis",
                *(jarvis.REPO_ROOT / group for group in jarvis.GROUPS)]
    assert roots == [path.resolve() for path in expected]
    assert all(root.is_dir() for root in roots), "a group folder named in GROUPS is missing"


def test_no_module_name_is_kept_in_two_folders():
    seen: dict[str, list[str]] = defaultdict(list)
    for entry in jarvis.__path__:
        folder = Path(entry)
        for name in _names_in(folder):
            seen[name].append(folder.name)
    clashes = {name: where for name, where in seen.items() if len(where) > 1}
    assert clashes == {}, f"the same module name in more than one folder: {clashes}"


def test_a_module_in_a_group_folder_is_reached_by_its_package_name():
    """The mechanism itself, end to end: a name resolves to the group folder that holds it."""
    durable = importlib.import_module("jarvis.durable")
    assert Path(durable.__file__).resolve() == (jarvis.REPO_ROOT / "background" / "durable.py").resolve()
    tools = importlib.import_module("jarvis.tools")
    assert Path(tools.__file__).resolve().parent == (jarvis.REPO_ROOT / "abilities" / "tools").resolve()
