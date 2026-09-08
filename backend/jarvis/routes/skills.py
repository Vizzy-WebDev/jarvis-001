"""Skills over HTTP.

**Only folder Skills.** Every route here reads `list_user_skills()`, which has no
code path that can return a built-in ability — the distinction is structural, not
a filter that could be forgotten. A screen listing "things Jarvis can do" is a
different question with a different answer, and it does not belong on this path.

The Node app also serves the built-in tool catalogue at `/api/skills`, from when
the two were one word. That route is deliberately NOT reproduced: it is the exact
shape of the mistake this build's Skills rule exists to prevent, and its contract
fixture reports as unported rather than being satisfied by something misleading.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..skills import files, install

router = APIRouter(prefix="/api/skills")


def _sync() -> None:
    """Keep the live declaration list in step with what is on disk."""
    from ..assembly import get_registry
    from ..skills.capabilities import sync

    sync(get_registry())


@router.get("/installed")
async def installed():
    return {"skills": files.list_user_skills()}


@router.get("/{name}")
async def detail(name: str):
    skill = files.get_skill(name)
    if skill is None:
        return JSONResponse({"ok": False, "error": "Unknown skill."}, status_code=404)

    document = files.read_skill_md(name)
    answer = {"ok": True, **skill, "raw": document["raw"], "body": document["body"],
              "supportingFiles": files.list_skill_files(name), "pipeline": None,
              "pipelineErrors": []}

    raw_toml = files.read_skill_toml(name)
    if raw_toml is not None:
        from ..skills import pipelines

        try:
            parsed = pipelines.parse(raw_toml)
        except ValueError as err:
            answer["pipelineErrors"] = [str(err)]
        else:
            errors = pipelines.validate(parsed)
            answer["pipelineErrors"] = errors
            answer["pipeline"] = {"description": parsed.description, "inputs": parsed.inputs,
                                  "steps": parsed.steps}
    return answer


@router.post("")
async def create(body: dict):
    from ..assembly import get_registry
    from ..capabilities import CapabilityKind

    reserved = {s.name for s in get_registry().list() if s.kind is not CapabilityKind.SKILL}
    try:
        skill = files.create_skill(name=str(body.get("name") or ""),
                                   description=str(body.get("description") or ""),
                                   instructions=str(body.get("instructions") or ""),
                                   reserved=reserved)
    except ValueError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    _sync()
    return {"ok": True, "skill": skill}


@router.patch("/{name}")
async def update(name: str, body: dict):
    if files.get_skill(name) is None:
        return JSONResponse({"ok": False, "error": "Unknown skill."}, status_code=404)

    if "enabled" in body:
        files.update_skill_state(name, {"enabled": bool(body["enabled"])})
    if "description" in body or "instructions" in body:
        try:
            files.update_skill_md(name, description=body.get("description"),
                                  instructions=body.get("instructions"))
        except ValueError as err:
            return JSONResponse({"ok": False, "error": str(err)}, status_code=400)
    _sync()
    return {"ok": True, "skill": files.get_skill(name)}


@router.delete("/{name}")
async def remove(name: str):
    if files.get_skill(name) is None:
        return JSONResponse({"ok": False, "error": "Unknown skill."}, status_code=404)
    files.delete_skill(name)
    _sync()
    return {"ok": True}


@router.post("/install")
async def install_skill(request: Request):
    """A repository link, a pasted SKILL.md, or a zip body.

    A zip arrives as raw bytes for the same reason an upload does; anything else
    is a small JSON body.
    """
    from ..assembly import get_registry
    from ..capabilities import CapabilityKind

    reserved = {s.name for s in get_registry().list() if s.kind is not CapabilityKind.SKILL}
    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            body = await request.json()
            if body.get("repo"):
                skill = install.install_from_github(str(body["repo"]), reserved=reserved)
            elif body.get("markdown"):
                skill = install.install_from_markdown(str(body["markdown"]),
                                                      reserved=reserved)
            else:
                return JSONResponse(
                    {"ok": False, "error": "Send a repository link or a SKILL.md."},
                    status_code=400)
        else:
            skill = install.install_from_zip(await request.body(), reserved=reserved)
    except install.InstallError as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=400)

    _sync()
    return {"ok": True, "skill": skill}


@router.get("/{name}/download")
async def download(name: str):
    if files.get_skill(name) is None:
        return JSONResponse({"ok": False, "error": "Unknown skill."}, status_code=404)
    return Response(
        content=install.export_zip(name), media_type="application/zip",
        # Forced, unconditionally: this serves back content the user (or
        # something they installed) wrote, and the same rule applies as to any
        # other served file.
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"',
                 "X-Content-Type-Options": "nosniff"})
