"""The one human-readable config file — `models.yaml` in the data directory — merged
over the shipped `data/defaults.yaml`, validated on load with clear errors.

It holds connections, aliases, routes per task class, policies, embedding spaces,
quirk profiles, prompt profiles and a few settings. Secrets are never in it: a
connection names its key by `secret_ref`, and the key itself lives in `.env`
through `config.save_secret`. Nothing here logs a value read from `.env`.

The settings screen edits this file through the small functions at the bottom.
Saving rewrites the whole file, so comments typed into it by hand do not survive
a save from the screen (recorded in DECISIONS.md).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .. import store
from . import capabilities as caps_mod
from . import drivers
from .catalog import Alias, Connection, Limits, ModelEntry, Pricing, split_endpoint_id
from .types import DATA_CLASSES, OPTIMIZE, TRUST_CLASSES

FILE_NAME = "models.yaml"
DEFAULTS_PATH = Path(__file__).resolve().parent / "data" / "defaults.yaml"

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
#: Keys that would mean a secret was typed into the config instead of `.env`.
_SECRET_KEYS = re.compile(r"(api[_-]?key|authorization|secret|password|token)$", re.IGNORECASE)
_SECTION_FORMATS = ("xml-tags", "markdown-headings")

_HEADER = """\
# Jarvis model layer configuration. See jarvis/models/README.md.
# Keys are never stored here: a connection's `secret_ref` names its key in .env.
# Saving from the Model Settings screen rewrites this file (comments are not kept).
"""


class ConfigError(ValueError):
    """Every problem found, one per line, each naming where it is."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("The model settings file has problems:\n" + "\n".join(f"- {p}" for p in problems))


@dataclass(frozen=True)
class Route:
    aliases: tuple[str, ...] = ()
    allow_others: bool = False
    optimize: str = "quality"


@dataclass(frozen=True)
class Policy:
    #: data class -> allowed trust classes. A data class not listed is unrestricted.
    data_classes: Mapping[str, frozenset[str]] = field(default_factory=dict, hash=False)
    monthly_budget_usd: float | None = None


@dataclass(frozen=True)
class EmbeddingSpace:
    name: str
    primary: str
    dimension: int
    #: Config declares these serve the identical model and version as the primary.
    backups: tuple[str, ...] = ()
    model_version: str | None = None


@dataclass(frozen=True)
class QuirkProfile:
    capabilities: Mapping[str, Any] = field(default_factory=dict, hash=False)
    wire: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class PromptProfile:
    section_format: str = "markdown-headings"
    tool_guidance: str | None = None


@dataclass(frozen=True)
class Settings:
    retries: int = 2
    retry_waits_s: tuple[float, ...] = (1.0, 3.0)
    repair_attempts: int = 2
    image_tokens: int = 1600
    breaker_threshold: int = 3
    breaker_base_s: float = 300.0
    breaker_max_s: float = 7200.0
    unreachable_rest_s: float = 300.0
    trace_content: bool = False


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    blurb: str
    driver: str | None
    base_url: str | None
    address: str
    key: str
    trust: str
    quirks: str | None = None
    default_params: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Config:
    connections: Mapping[str, Connection]
    aliases: Mapping[str, Alias]
    routes: Mapping[str, Route]
    policy: Policy
    embedding_spaces: Mapping[str, EmbeddingSpace]
    quirk_profiles: Mapping[str, QuirkProfile]
    prompt_profiles: Mapping[str, PromptProfile]
    settings: Settings
    presets: tuple[Preset, ...]
    driver_labels: Mapping[str, str]

    def route_for(self, task_class: str) -> Route:
        return self.routes.get(task_class) or self.routes.get("default") or Route(allow_others=True)

    def quirks_of(self, connection: Connection) -> QuirkProfile:
        return self.quirk_profiles.get(connection.quirks or "") or QuirkProfile()

    def prompt_profile(self, family: str | None) -> PromptProfile:
        return self.prompt_profiles.get(family or "") or self.prompt_profiles.get("default") or PromptProfile()


# --- reading ---------------------------------------------------------------------------------

def path() -> Path:
    return store.data_dir() / FILE_NAME


def _read_yaml(file: Path, problems: list[str], what: str) -> dict[str, Any]:
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        problems.append(f"{what}: couldn't be read ({err.strerror or err}).")
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        mark = getattr(err, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        problems.append(f"{what}: isn't valid YAML{where} ({getattr(err, 'problem', None) or err}).")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        problems.append(f"{what}: should be a set of named sections, not a {type(data).__name__}.")
        return {}
    return data


def _mapping(value: Any, where: str, problems: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where}: should be a set of named entries.")
        return {}
    return value


def _number(value: Any, where: str, problems: list[str], *, allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        problems.append(f"{where}: should be a number, zero or more.")
        return None
    return float(value)


def _whole(value: Any, where: str, problems: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        problems.append(f"{where}: should be a whole number above zero.")
        return None
    return value


def _no_secrets(value: Any, where: str, problems: list[str]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str) and _SECRET_KEYS.search(key):
                problems.append(f"{where}.{key}: looks like a secret. Keys belong in .env "
                                "(named by the connection's secret_ref), never in this file.")
            _no_secrets(sub, f"{where}.{key}", problems)


def _capabilities(raw: Any, where: str, problems: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in _mapping(raw, where, problems).items():
        try:
            caps_mod.check_name(str(name))
            caps_mod.check_value(str(name), value)
        except ValueError as err:
            problems.append(f"{where}.{name}: {err}")
            continue
        out[str(name)] = value
    return out


def _pricing(raw: Any, where: str, problems: list[str]) -> Pricing | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or "input" not in raw or "output" not in raw:
        problems.append(f"{where}: needs `input` and `output`, in US dollars per million tokens.")
        return None
    values = [_number(raw.get(k), f"{where}.{k}", problems, allow_none=(k == "cached_input"))
              for k in ("input", "output", "cached_input")]
    if values[0] is None or values[1] is None:
        return None
    return Pricing(values[0], values[1], values[2])


def _model_entry(raw: Any, where: str, problems: list[str]) -> ModelEntry:
    raw = _mapping(raw, where, problems)
    unknown = set(raw) - {"capabilities", "pricing", "context", "family", "upstream", "label", "latency_ms"}
    for key in sorted(unknown):
        problems.append(f"{where}.{key}: isn't a setting a model can have.")
    return ModelEntry(
        capabilities=_capabilities(raw.get("capabilities"), f"{where}.capabilities", problems),
        pricing=_pricing(raw.get("pricing"), f"{where}.pricing", problems),
        context=_whole(raw.get("context"), f"{where}.context", problems),
        family=str(raw["family"]) if raw.get("family") else None,
        upstream=str(raw["upstream"]) if raw.get("upstream") else None,
        label=str(raw["label"]) if raw.get("label") else None,
        latency_ms=_whole(raw.get("latency_ms"), f"{where}.latency_ms", problems),
    )


def _url_problem(url: Any) -> str | None:
    if not isinstance(url, str) or not re.match(r"^https?://[^/\s]+", url.strip()):
        return "should be a web address starting with http:// or https://"
    return None


_CONNECTION_KEYS = {"name", "driver", "base_url", "trust", "secret_ref", "limits", "quirks",
                    "default_params", "discovery", "models", "label", "preset"}


def _connection(raw: Any, index: int, quirks: Mapping[str, QuirkProfile],
                problems: list[str]) -> Connection | None:
    where = f"connections[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: should be a set of settings.")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        problems.append(f"{where}.name: needs a short name of lowercase letters, digits, '.', '_' or '-' "
                        "(no '/'), like home-server.")
        return None
    where = f"connections.{name}"
    for key in sorted(set(raw) - _CONNECTION_KEYS):
        problems.append(f"{where}.{key}: isn't a setting a connection can have.")
    driver = raw.get("driver")
    if driver not in drivers.DRIVERS:
        problems.append(f"{where}.driver: “{driver}” isn't a driver. Use one of: {', '.join(sorted(drivers.DRIVERS))}.")
        return None
    problem = _url_problem(raw.get("base_url"))
    if problem:
        problems.append(f"{where}.base_url: {problem}.")
    trust = raw.get("trust")
    if trust not in TRUST_CLASSES:
        problems.append(f"{where}.trust: should be one of {', '.join(TRUST_CLASSES)}.")
    profile = raw.get("quirks")
    if profile is not None and profile not in quirks:
        problems.append(f"{where}.quirks: there's no quirk profile called “{profile}”.")
    elif profile is not None:
        known = getattr(drivers.get(driver), "QUIRKS", frozenset())
        for flag in quirks[profile].wire:
            if flag not in known:
                problems.append(f"{where}.quirks: profile “{profile}” sets “{flag}”, which the {driver} "
                                "driver doesn't understand.")
    secret_ref = raw.get("secret_ref")
    if secret_ref is not None and (not isinstance(secret_ref, str) or not re.match(r"^[A-Za-z0-9_]+$", secret_ref)):
        problems.append(f"{where}.secret_ref: should be the NAME of a saved key (letters, digits, _), not the key.")
    default_params = _mapping(raw.get("default_params"), f"{where}.default_params", problems)
    _no_secrets(default_params, f"{where}.default_params", problems)
    limits_raw = _mapping(raw.get("limits"), f"{where}.limits", problems)
    for key in sorted(set(limits_raw) - {"concurrency", "rpm"}):
        problems.append(f"{where}.limits.{key}: only concurrency and rpm can be set.")
    models: dict[str, ModelEntry] = {}
    for model_id, entry in _mapping(raw.get("models"), f"{where}.models", problems).items():
        models[str(model_id)] = _model_entry(entry, f"{where}.models.{model_id}", problems)
    discovery = raw.get("discovery", True)
    if not isinstance(discovery, bool):
        problems.append(f"{where}.discovery: should be true or false.")
        discovery = True
    return Connection(
        name=name, driver=driver, base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        trust=trust if trust in TRUST_CLASSES else "standard",
        secret_ref=secret_ref if isinstance(secret_ref, str) else None,
        limits=Limits(concurrency=_whole(limits_raw.get("concurrency"), f"{where}.limits.concurrency", problems),
                      rpm=_whole(limits_raw.get("rpm"), f"{where}.limits.rpm", problems)),
        quirks=profile if isinstance(profile, str) else None,
        default_params=default_params, discovery=discovery, models=models,
        label=str(raw["label"]) if raw.get("label") else None,
        preset=str(raw["preset"]) if raw.get("preset") else None,
    )


def parse(user: Mapping[str, Any], defaults: Mapping[str, Any] | None = None) -> Config:
    """Build a Config from the two raw documents, or raise ConfigError naming every problem."""
    problems: list[str] = []
    defaults = defaults if defaults is not None else load_defaults()

    def merged(key: str) -> dict[str, Any]:
        out = dict(_mapping(defaults.get(key), f"defaults.{key}", problems))
        out.update(_mapping(user.get(key), key, problems))
        return out

    for key in sorted(set(user) - {"version", "connections", "aliases", "routes", "policies",
                                   "embedding_spaces", "quirk_profiles", "prompt_profiles", "settings"}):
        problems.append(f"{key}: isn't a section this file can have.")

    quirk_profiles: dict[str, QuirkProfile] = {}
    for name, raw in merged("quirk_profiles").items():
        raw = _mapping(raw, f"quirk_profiles.{name}", problems)
        quirk_profiles[str(name)] = QuirkProfile(
            capabilities=_capabilities(raw.get("capabilities"), f"quirk_profiles.{name}.capabilities", problems),
            wire=_mapping(raw.get("wire"), f"quirk_profiles.{name}.wire", problems))

    prompt_profiles: dict[str, PromptProfile] = {}
    for name, raw in merged("prompt_profiles").items():
        raw = _mapping(raw, f"prompt_profiles.{name}", problems)
        fmt = raw.get("section_format", "markdown-headings")
        if fmt not in _SECTION_FORMATS:
            problems.append(f"prompt_profiles.{name}.section_format: should be one of {', '.join(_SECTION_FORMATS)}.")
            fmt = "markdown-headings"
        guidance = raw.get("tool_guidance")
        prompt_profiles[str(name)] = PromptProfile(section_format=fmt,
                                                   tool_guidance=str(guidance) if guidance else None)

    connections: dict[str, Connection] = {}
    raw_connections = user.get("connections") or []
    if not isinstance(raw_connections, list):
        problems.append("connections: should be a list.")
        raw_connections = []
    for index, raw in enumerate(raw_connections):
        conn = _connection(raw, index, quirk_profiles, problems)
        if conn is None:
            continue
        if conn.name in connections:
            problems.append(f"connections.{conn.name}: that name is used twice.")
            continue
        connections[conn.name] = conn

    aliases: dict[str, Alias] = {}
    for name, raw in _mapping(user.get("aliases"), "aliases", problems).items():
        where = f"aliases.{name}"
        raw = raw if isinstance(raw, dict) else {"endpoint": raw} if isinstance(raw, str) else {}
        endpoint, family = raw.get("endpoint"), raw.get("family")
        if bool(endpoint) == bool(family):
            problems.append(f"{where}: needs exactly one of `endpoint` (connection/model-id) or `family`.")
            continue
        if endpoint:
            try:
                conn_name, _ = split_endpoint_id(str(endpoint))
            except ValueError as err:
                problems.append(f"{where}.endpoint: {err}")
                continue
            # A connection that has since been removed is not an error here: the alias
            # stays, and whoever uses it is told the model isn't set up any more.
        aliases[str(name)] = Alias(str(name), endpoint=str(endpoint) if endpoint else None,
                                   family=str(family) if family else None)

    routes: dict[str, Route] = {}
    for name, raw in merged("routes").items():
        where = f"routes.{name}"
        raw = _mapping(raw, where, problems)
        listed = raw.get("aliases") or []
        if not isinstance(listed, list):
            problems.append(f"{where}.aliases: should be a list of alias names.")
            listed = []
        for alias in listed:
            if alias not in aliases:
                problems.append(f"{where}.aliases: there's no alias called “{alias}”.")
        optimize = raw.get("optimize", "quality")
        if optimize not in OPTIMIZE:
            problems.append(f"{where}.optimize: should be one of {', '.join(OPTIMIZE)}.")
            optimize = "quality"
        routes[str(name)] = Route(aliases=tuple(str(a) for a in listed if a in aliases),
                                  allow_others=bool(raw.get("allow_others", False)), optimize=optimize)

    policies = merged("policies")
    data_classes: dict[str, frozenset[str]] = {}
    for dc, allowed in _mapping(policies.get("data_classes"), "policies.data_classes", problems).items():
        if dc not in DATA_CLASSES:
            problems.append(f"policies.data_classes.{dc}: isn't a data class ({', '.join(DATA_CLASSES)}).")
            continue
        if not isinstance(allowed, list) or not allowed or any(t not in TRUST_CLASSES for t in allowed):
            problems.append(f"policies.data_classes.{dc}: should list trust classes from "
                            f"{', '.join(TRUST_CLASSES)}.")
            continue
        data_classes[dc] = frozenset(allowed)
    policy = Policy(data_classes=data_classes,
                    monthly_budget_usd=_number(policies.get("monthly_budget_usd"),
                                               "policies.monthly_budget_usd", problems))

    spaces: dict[str, EmbeddingSpace] = {}
    for name, raw in _mapping(user.get("embedding_spaces"), "embedding_spaces", problems).items():
        where = f"embedding_spaces.{name}"
        raw = _mapping(raw, where, problems)
        primary, backups = raw.get("primary"), raw.get("backups") or []
        dimension = _whole(raw.get("dimension"), f"{where}.dimension", problems)
        ok = True
        for label, value in [("primary", primary)] + [("backups", b) for b in backups]:
            try:
                conn_name, _ = split_endpoint_id(str(value or ""))
            except ValueError as err:
                problems.append(f"{where}.{label}: {err}")
                ok = False
                continue
            if conn_name not in connections:
                problems.append(f"{where}.{label}: there's no connection called “{conn_name}”.")
                ok = False
        if ok and dimension:
            spaces[str(name)] = EmbeddingSpace(str(name), str(primary), dimension, tuple(str(b) for b in backups),
                                               str(raw["model_version"]) if raw.get("model_version") else None)

    raw_settings = merged("settings")
    defaults_settings = Settings()
    settings_values: dict[str, Any] = {}
    for key, value in raw_settings.items():
        if not hasattr(defaults_settings, key):
            problems.append(f"settings.{key}: isn't a setting.")
            continue
        expected = getattr(defaults_settings, key)
        if isinstance(expected, bool):
            if not isinstance(value, bool):
                problems.append(f"settings.{key}: should be true or false.")
                continue
        elif isinstance(expected, tuple):
            if not isinstance(value, list) or any(_number(v, f"settings.{key}", problems) is None for v in value):
                problems.append(f"settings.{key}: should be a list of numbers.")
                continue
            value = tuple(float(v) for v in value)
        elif _number(value, f"settings.{key}", problems) is None:
            continue
        settings_values[key] = value
    settings = Settings(**settings_values)

    presets: list[Preset] = []
    for raw in defaults.get("presets") or []:
        presets.append(Preset(
            id=raw["id"], label=raw["label"], blurb=raw.get("blurb", ""), driver=raw.get("driver"),
            base_url=raw.get("base_url"), address=raw.get("address", "required"), key=raw.get("key", "optional"),
            trust=raw.get("trust", "standard"), quirks=raw.get("quirks"),
            default_params=raw.get("default_params") or {}))

    if problems:
        raise ConfigError(problems)
    return Config(connections=connections, aliases=aliases, routes=routes, policy=policy,
                  embedding_spaces=spaces, quirk_profiles=quirk_profiles, prompt_profiles=prompt_profiles,
                  settings=settings, presets=tuple(presets),
                  driver_labels=dict(defaults.get("driver_labels") or {}))


_defaults_cache: dict[str, Any] | None = None


def load_defaults() -> dict[str, Any]:
    global _defaults_cache
    if _defaults_cache is None:
        problems: list[str] = []
        _defaults_cache = _read_yaml(DEFAULTS_PATH, problems, "defaults.yaml")
        if problems:
            raise ConfigError(problems)
    return _defaults_cache


_lock = threading.RLock()
_cache: tuple[str, float, Config] | None = None
_listeners: list[Callable[[], None]] = []


def on_change(fn: Callable[[], None]) -> None:
    """Called after the file is saved from here or found changed on disk."""
    _listeners.append(fn)


def _stamp(file: Path) -> float:
    try:
        return file.stat().st_mtime_ns
    except FileNotFoundError:
        return -1


def raw() -> dict[str, Any]:
    """The file as written, for an edit to change and save back."""
    problems: list[str] = []
    data = _read_yaml(path(), problems, FILE_NAME)
    if problems:
        raise ConfigError(problems)
    return data


def current() -> Config:
    """The config now on disk. Re-read when the file changes (a hand edit counts)."""
    global _cache
    file = path()
    with _lock:
        stamp = _stamp(file)
        if _cache is not None and _cache[0] == str(file) and _cache[1] == stamp:
            return _cache[2]
        parsed = parse(raw())
        changed = _cache is not None
        _cache = (str(file), stamp, parsed)
    if changed:
        for fn in list(_listeners):
            fn()
    return parsed


def forget() -> None:
    """Test helper: drop the cached parse (a new data directory, say)."""
    global _cache
    with _lock:
        _cache = None


# --- writing ---------------------------------------------------------------------------------

def save(data: Mapping[str, Any]) -> Config:
    """Validate, then write atomically. Nothing invalid ever reaches the disk."""
    parsed = parse(data)
    file = path()
    text = _HEADER + yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True, default_flow_style=False)
    tmp = file.with_name(f"{file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _lock:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, file)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                import time

                time.sleep(0.02)
        forget()
    for fn in list(_listeners):
        fn()
    return parsed


def edit(change: Callable[[dict[str, Any]], None]) -> Config:
    """Read the file, let `change` alter the raw document, validate and save it."""
    with _lock:
        data = raw()
        change(data)
        return save(data)


def _connections(data: dict[str, Any]) -> list[dict[str, Any]]:
    data.setdefault("connections", [])
    return data["connections"]


def find_connection(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((c for c in _connections(data) if isinstance(c, dict) and c.get("name") == name), None)


def unique_name(base: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", base.lower()).strip("-._") or "connection"
    slug = slug[:50]
    name, n = slug, 2
    while name in taken:
        name, n = f"{slug}-{n}", n + 1
    return name


def add_connection(entry: dict[str, Any]) -> Config:
    def change(data: dict[str, Any]) -> None:
        _connections(data).append(entry)
    return edit(change)


def update_connection(name: str, **fields: Any) -> Config:
    def change(data: dict[str, Any]) -> None:
        found = find_connection(data, name)
        if found is None:
            raise KeyError(name)
        for key, value in fields.items():
            if value is None:
                found.pop(key, None)
            else:
                found[key] = value
    return edit(change)


def remove_connection(name: str) -> Config:
    def change(data: dict[str, Any]) -> None:
        data["connections"] = [c for c in _connections(data) if not (isinstance(c, dict) and c.get("name") == name)]
    return edit(change)


def set_model(connection: str, model_id: str, entry: dict[str, Any] | None = None) -> Config:
    def change(data: dict[str, Any]) -> None:
        found = find_connection(data, connection)
        if found is None:
            raise KeyError(connection)
        models = found.setdefault("models", {}) or {}
        found["models"] = models
        models[model_id] = entry if entry is not None else models.get(model_id) or {}
    return edit(change)


def remove_model(connection: str, model_id: str) -> Config:
    def change(data: dict[str, Any]) -> None:
        found = find_connection(data, connection)
        if found is None:
            raise KeyError(connection)
        (found.get("models") or {}).pop(model_id, None)
    return edit(change)


def set_alias(name: str, *, endpoint: str | None = None, family: str | None = None) -> Config:
    def change(data: dict[str, Any]) -> None:
        aliases = data.setdefault("aliases", {}) or {}
        data["aliases"] = aliases
        aliases[name] = {"endpoint": endpoint} if endpoint else {"family": family}
    return edit(change)


def remove_alias(name: str) -> Config:
    def change(data: dict[str, Any]) -> None:
        (data.get("aliases") or {}).pop(name, None)
        for route in (data.get("routes") or {}).values():
            if isinstance(route, dict) and isinstance(route.get("aliases"), list) and name in route["aliases"]:
                route["aliases"] = [a for a in route["aliases"] if a != name]
    return edit(change)
