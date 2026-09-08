"""Launch a program the user already has installed.

**The safety property, unchanged from the original and worth stating plainly:
the model only ever supplies a NAME, never a command.** What actually runs is
something this file resolved itself — an entry in its own fixed map, or a real
shortcut file already sitting in the user's Start Menu. Model-authored text never
reaches a shell, which is §35's requirement (never a hidden unrestricted shell
controlled by model output) applied to the one tool most tempted to break it.

Windows is the target platform. On anything else this reports honestly that it
cannot launch desktop apps rather than pretending or half-trying (§45).
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from ..capabilities import CapabilitySpec, Risk

#: A plain command (resolved by Windows' own `start`, which checks PATH and the
#: App Paths registry) or a protocol URI.
ALLOWLIST = {
    "notepad": "notepad", "text editor": "notepad",
    "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "ms paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "settings": "ms-settings:", "windows settings": "ms-settings:",
    "task manager": "taskmgr",
    "command prompt": "cmd", "cmd": "cmd", "terminal": "cmd",
    "control panel": "control",
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "microsoft edge": "msedge",
    "word": "winword", "microsoft word": "winword",
    "excel": "excel", "microsoft excel": "excel",
    "powerpoint": "powerpnt", "microsoft powerpoint": "powerpnt",
    "spotify": "spotify:",
}

#: Generous, so a pathological Start Menu cannot hang the turn.
MAX_SHORTCUTS_SCANNED = 2000


def start_menu_dirs() -> list[Path]:
    roots = [os.environ.get("ProgramData"), os.environ.get("APPDATA")]
    return [Path(r) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            for r in roots if r]


def list_shortcuts(dirs: list[Path] | None = None) -> list[dict[str, str]]:
    """Pure enough to test without launching anything — the matching logic is
    the part worth testing, and it must not require a real Start Menu."""
    found: list[dict[str, str]] = []
    for directory in (dirs if dirs is not None else start_menu_dirs()):
        if not directory.is_dir():
            continue
        try:
            for path in directory.rglob("*.lnk"):
                if len(found) >= MAX_SHORTCUTS_SCANNED:
                    return found
                found.append({"name": path.stem, "path": str(path)})
        except OSError:
            continue        # a locked-down folder is a skip, not a failure
    return found


def best_match(query: str, shortcuts: list[dict[str, str]]) -> dict[str, str] | None:
    """Exact match first, then the SHORTEST containing/contained name — which
    favours "code" -> "Visual Studio Code" over an unrelated longer name that
    happens to share a substring."""
    wanted = query.strip().lower()
    for shortcut in shortcuts:
        if shortcut["name"].lower() == wanted:
            return shortcut
    partial = [s for s in shortcuts
               if wanted in s["name"].lower() or s["name"].lower() in wanted]
    return min(partial, key=lambda s: len(s["name"])) if partial else None


def _launch(target: str) -> None:
    # `start` with an empty title argument, and the target as its own argv
    # entry. Nothing here is built from model text.
    subprocess.Popen(["cmd.exe", "/c", "start", "", target],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_by_name(name: str) -> str:
    """Resolve a friendly name the same way this tool does, and start it.

    Exported so a control session can open an app through the ONE resolver that
    decides what a name may become, rather than growing a second, slightly
    different idea of what "notepad" means. Raises rather than returning a result
    dict: its caller is the control loop, which reports failures its own way.
    """
    wanted = str(name or "").strip()
    if not wanted:
        raise ValueError("No app name given.")
    known = ALLOWLIST.get(wanted.lower())
    if known:
        _launch(known)
        return wanted
    match = best_match(wanted, list_shortcuts())
    if match is None:
        raise ValueError(f'I couldn\'t find an app called "{wanted}" on this computer.')
    _launch(match["path"])
    return match["name"]


def _run(name: str = "") -> dict:
    wanted = str(name or "").strip()
    if not wanted:
        return {"ok": False, "error": "No app name given."}
    if platform.system() != "Windows":
        return {"ok": False,
                "error": "I can only launch desktop apps on Windows, and this isn't Windows."}

    known = ALLOWLIST.get(wanted.lower())
    if known:
        _launch(known)
        return {"ok": True, "opened": wanted, "speak": f"Opening {wanted}."}

    match = best_match(wanted, list_shortcuts())
    if match is None:
        known_names = ", ".join(sorted(set(ALLOWLIST.values())))
        return {"ok": False,
                "error": f'I couldn\'t find "{wanted}" — it isn\'t one of the apps I know by '
                         f"name ({known_names}), and no matching shortcut turned up in your "
                         "Start Menu."}
    _launch(match["path"])
    return {"ok": True, "opened": match["name"], "speak": f"Opening {match['name']}."}


SPEC = CapabilitySpec(
    id="builtin.open_app",
    name="open_app",
    description=("Launch a program installed on the user's computer — anything in their Start "
                 "Menu, not just a fixed list. Only for desktop apps, not websites."),
    input_schema={"type": "object", "properties": {
        "name": {"type": "string",
                 "description": 'The app to open, e.g. "notepad" or "Visual Studio Code".'}},
        "required": ["name"]},
    # §7's own example of a LOW-risk action. It starts something the user
    # already installed and changes nothing.
    risk=Risk.LOW,
    handler=_run,
    timeout_s=15.0,
    tags=frozenset({"core"}),
)
