"""Adapt: turn a request into what one chosen endpoint's driver is handed.

* Instruction sections are rendered with the family's prompt profile (XML tags
  or markdown headings, plus any tool-use guidance it carries).
* Tool schemas were already translated during resolve (an inexpressible one made
  the endpoint ineligible there, so `explain_route` knows about it too).
* `stable_prefix_until` becomes a canonical "cache the stable prefix" flag and
  `reasoning_effort` a canonical level — each only if the endpoint has the
  capability, otherwise reported as dropped. The native encoding is the driver's.
* Sealed items from any other endpoint are left out, and reported.
* For a model that can't see, a picture from earlier in the conversation becomes a
  short placeholder note, reported (only a current-turn picture requires `image_in`).
* `default_params`, then this driver's `extensions`, become the driver's params.
  An extension for a different driver is ignored with a warning.

Nothing else here changes, trims or rewrites the caller's content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import capabilities as caps_mod
from .config import Config
from .prepared import Prepared, PreparedOutput, RenderedSection, deep_merge
from .resolve import Candidate
from .types import FeatureState, ImagePart, Message, Request, Sealed, TextPart

#: What a model that can't see gets in place of a picture shared earlier in the conversation.
PICTURE_PLACEHOLDER = "[A picture was shared here. This model can't see pictures.]"


@dataclass
class Report:
    features: dict[str, FeatureState] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _tag(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "section"


def render(label: str, text: str, section_format: str) -> str:
    if section_format == "xml-tags":
        tag = _tag(label)
        return f"<{tag}>\n{text}\n</{tag}>"
    heading = label.replace("_", " ").strip()
    return f"## {heading[:1].upper()}{heading[1:]}\n\n{text}"


def output_instruction(schema: Any) -> str:
    return ("Reply with one JSON object and nothing else — no commentary and no code fence. "
            "It must match this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False, indent=2))


def prepare(request: Request, candidate: Candidate, cfg: Config, *, mint_id: Callable[[], str],
            report: Report) -> Prepared:
    endpoint, connection = candidate.endpoint, candidate.connection
    caps = endpoint.capabilities
    profile = cfg.prompt_profile(endpoint.family)

    sections: list[RenderedSection] = []
    stable_until = request.hints.stable_prefix_until
    stable = stable_until is not None
    for section in request.instructions:
        sections.append(RenderedSection(section.label, render(section.label, section.text, profile.section_format),
                                        stable=stable))
        if section.label == stable_until:
            stable = False
    if request.tools and profile.tool_guidance:
        sections.append(RenderedSection("tool_use", render("tool_use", profile.tool_guidance,
                                                           profile.section_format)))

    output = PreparedOutput()
    if request.output.kind == "json":
        output = PreparedOutput(mode=candidate.output_mode, schema=candidate.output_schema)
        if candidate.emulated:
            sections.append(RenderedSection("output_format", render("output_format",
                                                                    output_instruction(candidate.output_schema),
                                                                    profile.section_format)))
            report.features["structured_output"] = "emulated"
        else:
            report.features["structured_output"] = "native"

    cache = False
    if stable_until is not None:
        cache = caps_mod.has(caps, "prompt_caching")
        report.features["prompt_caching"] = "native" if cache else "dropped"

    effort = None
    if request.hints.reasoning_effort is not None:
        if caps_mod.has(caps, "reasoning_control"):
            effort = request.hints.reasoning_effort
            report.features["reasoning_effort"] = "native"
        else:
            report.features["reasoning_effort"] = "dropped"

    if request.hints.max_output_tokens is not None:
        report.features["max_output_tokens"] = "native"
    if request.tools:
        report.features["tools"] = "native"
    sees = caps_mod.has(caps, "image_in")
    if any(isinstance(p, ImagePart) for i in request.items if isinstance(i, Message) for p in i.parts):
        report.features["image_input"] = "native" if sees else "dropped"

    items = []
    foreign = 0
    pictures_left_out = 0
    for item in request.items:
        if isinstance(item, Sealed) and item.endpoint_id != endpoint.id:
            foreign += 1
            continue
        if not sees and isinstance(item, Message) and any(isinstance(p, ImagePart) for p in item.parts):
            # Only an earlier picture can be here (resolve requires image_in for one in the
            # current turn): it becomes a short note, so the model knows one was shared.
            pictures_left_out += sum(isinstance(p, ImagePart) for p in item.parts)
            item = Message(item.role, tuple(TextPart(PICTURE_PLACEHOLDER) if isinstance(p, ImagePart) else p
                                            for p in item.parts))
        items.append(item)
    if pictures_left_out:
        report.warnings.append(f"{pictures_left_out} earlier picture{'s were' if pictures_left_out != 1 else ' was'} "
                               "left out, each replaced by a short note: this model can't see pictures.")
    if foreign:
        report.features["foreign_provider_state"] = "dropped"
        report.warnings.append(f"{foreign} piece{'s' if foreign != 1 else ''} of provider state (reasoning or "
                               f"signatures) from another endpoint {'were' if foreign != 1 else 'was'} left out: "
                               f"only the endpoint that made it can read it.")

    params = dict(connection.default_params)
    for namespace, options in request.extensions.items():
        if namespace == connection.driver:
            params = deep_merge(params, options)
            report.features[f"extensions.{namespace}"] = "native"
        else:
            report.features[f"extensions.{namespace}"] = "dropped"
            report.warnings.append(f"Options for “{namespace}” were ignored: this call went to a "
                                   f"{connection.driver} endpoint.")

    return Prepared(
        endpoint_id=endpoint.id, model_id=endpoint.model_id, system=tuple(sections), items=tuple(items),
        tools=candidate.tools, output=output, effort=effort, cache=cache,
        parallel_tools=caps_mod.has(caps, "parallel_tools"),
        max_output_tokens=request.hints.max_output_tokens, params=params,
        caps=caps_mod.plain(caps), mint_id=mint_id,
    )
