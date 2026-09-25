"""The artifact routes and the guard that keeps an artifact's own page from using them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis import artifacts, chat_store
from jarvis.db import reset_for_tests as reset_db
from jarvis.main import create_app
from jarvis.tools.create_artifact import _run


class _Ctx:
    def __init__(self, session_id):
        self.session_id = session_id


@pytest.fixture(autouse=True)
def _isolate(scratch):
    reset_db()
    yield
    reset_db()


@pytest.fixture
def client():
    return TestClient(create_app())


def _make(name, content="x", session=None, **extra):
    result = _run(filename=name, content=content, ctx=_Ctx(session), **extra)
    assert result["ok"], result
    return result["id"]


# --- listing ------------------------------------------------------------------------------------

def test_an_empty_list_keeps_the_recorded_shape(client):
    assert client.get("/api/artifacts").json() == {"artifacts": []}


def test_each_item_says_what_it_is_and_which_chat_it_came_from(client):
    chat = chat_store.create_conversation()
    chat_store.rename_conversation(chat["id"], "Planning the menu")
    made = _make("menu.md", "# Menu", session=chat["id"], title="Winter menu")
    [item] = client.get("/api/artifacts").json()["artifacts"]
    assert item["id"] == made and item["title"] == "Winter menu" and item["kind"] == "markdown"
    assert item["createdAt"] and item["url"] == f"/api/artifacts/{made}"
    assert item["conversation"] == {"id": chat["id"], "title": "Planning the menu", "state": "live"}


def test_the_chat_state_is_trashed_or_gone_when_it_was_deleted(client):
    trashed, gone = chat_store.create_conversation()["id"], chat_store.create_conversation()["id"]
    a = _make("a.md", session=trashed)
    b = _make("b.md", session=gone)
    chat_store.delete_conversation(trashed)
    chat_store.purge_conversation(gone)
    items = {i["id"]: i for i in client.get("/api/artifacts").json()["artifacts"]}
    assert items[a]["conversation"]["state"] == "trashed"
    assert items[b]["conversation"] == {"id": gone, "title": None, "state": "gone"}


def test_a_job_made_file_has_no_chat(client):
    _make("job.md", session="job:abc")
    assert client.get("/api/artifacts").json()["artifacts"][0]["conversation"] is None


def test_paging_search_and_kind_filter_over_http(client):
    for i in range(7):
        _make(f"note{i}.md")
    _make("script.py", "print(1)")
    first = client.get("/api/artifacts", params={"limit": 5}).json()
    assert len(first["artifacts"]) == 5 and first["nextBefore"]
    rest = client.get("/api/artifacts", params={"limit": 5, "before": first["nextBefore"]}).json()
    assert len(rest["artifacts"]) == 3 and "nextBefore" not in rest
    assert [a["name"] for a in client.get("/api/artifacts", params={"kind": "code"}).json()["artifacts"]] == ["script.py"]
    assert [a["name"] for a in client.get("/api/artifacts", params={"q": "note3"}).json()["artifacts"]] == ["note3.md"]


# --- one item, preview, file, delete ------------------------------------------------------------

def test_info_and_a_missing_one(client):
    made = _make("x.md")
    assert client.get(f"/api/artifacts/{made}/info").json()["artifact"]["id"] == made
    assert client.get("/api/artifacts/art_nope/info").status_code == 404


def test_a_word_document_previews_as_text_never_as_the_file(client):
    made = _run(filename="r.docx", paragraphs=["Quarterly report", "Sales rose."], ctx=_Ctx(None))["id"]
    response = client.get(f"/api/artifacts/{made}/preview")
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["format"] == "markdown" and "Sales rose." in body["markdown"]


def test_a_spreadsheet_previews_as_rows(client):
    made = _run(filename="s.xlsx", rows=[["name", "score"], ["ann", 3]], ctx=_Ctx(None))["id"]
    body = client.get(f"/api/artifacts/{made}/preview").json()
    assert body["format"] == "sheets" and body["sheets"][0]["rows"][:2] == [["name", "score"], ["ann", "3"]]


def test_a_presentation_previews_as_text(client):
    made = _run(filename="d.pptx", slides=[{"title": "Intro", "bullets": ["one"]}], ctx=_Ctx(None))["id"]
    body = client.get(f"/api/artifacts/{made}/preview").json()
    assert "Intro" in body["markdown"] and "one" in body["markdown"]


def test_text_has_no_preview_route_it_is_read_directly(client):
    assert client.get(f"/api/artifacts/{_make('a.md')}/preview").status_code == 400


def test_the_file_is_still_always_a_download(client):
    made = _make("evil.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    response = client.get(f"/api/artifacts/{made}")
    assert response.headers["content-disposition"] == 'attachment; filename="evil.svg"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_delete_removes_it_everywhere(client):
    made = _make("gone.md", "# bye")
    path = artifacts.get(made).path
    assert client.delete(f"/api/artifacts/{made}").json() == {"ok": True}
    assert not path.exists()
    assert client.get(f"/api/artifacts/{made}").status_code == 404
    assert client.get("/api/artifacts").json() == {"artifacts": []}
    assert client.delete(f"/api/artifacts/{made}").status_code == 404


# --- the guard ----------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                                     {"Origin": "null", "Sec-Fetch-Site": "same-origin"}])
def test_a_request_from_a_page_outside_jarvis_is_refused(client, headers):
    for method, path in (("GET", "/api/artifacts"), ("GET", "/api/chat/stream?message=hi"),
                         ("POST", "/api/conversations"), ("DELETE", "/api/artifacts/x")):
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403, (method, path)
        assert "outside Jarvis" in response.json()["error"]


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-origin"},
                                     {"Sec-Fetch-Site": "none"},
                                     {"Origin": "http://127.0.0.1:3000", "Sec-Fetch-Site": "same-origin"}])
def test_the_app_itself_a_typed_url_and_a_script_are_not(client, headers):
    assert client.get("/api/artifacts", headers=headers).status_code == 200


def test_the_guard_leaves_the_front_end_itself_alone(client):
    # The page shell is not API: a foreign navigation to it just loads the app,
    # which (inside a sandboxed frame) still cannot call the API.
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code in (200, 404)


def test_a_foreign_websocket_is_closed(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/duplex", headers={"Origin": "null"}) as ws:
            ws.receive_text()
