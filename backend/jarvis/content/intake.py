"""Working out what something IS, for free.

`identify()` makes NO model call, ever. That is the load-bearing part of this
package: it answers "what is this" from cheap metadata — YouTube's public oEmbed
endpoint, a page's own title, a file's size on disk — which is what lets Jarvis
say "that's a 40-minute video about X, what do you want from it?" and then
genuinely wait, rather than reading the thing first and asking afterwards.

The other half is `prepare_for()`: putting a source in front of a model that can
actually watch or see it, cheapest route first.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..media import file_kind, human_size, inline_attachment, mime_type_for, TooBig
from ..webtext import BROWSER_UA, fetch_article

logger = logging.getLogger(__name__)

#: YouTube serves a cookie-consent interstitial instead of the watch page to a
#: request carrying no consent cookie. These are what a browser would send.
YOUTUBE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+000; SOCS=CAI",
}

TRANSCRIPT_MAX_CHARS = 40000
_YOUTUBE_HOST = re.compile(r"^(www\.|m\.|music\.)?(youtube\.com|youtu\.be)$", re.I)
_YOUTUBE_PATH = re.compile(r"^/(shorts|embed|live|v)/([^/?#]+)")
_WINDOWS_PATH = re.compile(r"^[a-zA-Z]:[\\/]")


# --- what did the user actually hand over -------------------------------------

def youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.hostname or not _YOUTUBE_HOST.match(parsed.hostname):
        return None
    if parsed.hostname.lower().endswith("youtu.be"):
        return parsed.path.lstrip("/").split("/")[0] or None
    query = parse_qs(parsed.query or "")
    if query.get("v"):
        return query["v"][0]
    match = _YOUTUBE_PATH.match(parsed.path or "")
    return match.group(2) if match else None


def classify_source(text: str) -> dict[str, Any]:
    """A YouTube link, a web page, a file path, or plain text.

    Deliberately forgiving: it is fed by a text box AND a voice transcript, so it
    has to cope with a spoken path, a pasted link with tracking junk on it, or a
    wall of text with no marker at all. Purely syntactic — no network, no
    filesystem check, so it is safe to call on anything.
    """
    raw = (text or "").strip()
    if not raw:
        return {"kind": "text", "text": ""}

    video_id = youtube_video_id(raw)
    if video_id:
        return {"kind": "youtube", "url": raw, "videoId": video_id}

    if re.match(r"^https?://", raw, re.I):
        parsed = urlparse(raw)
        if parsed.netloc:
            return {"kind": "url", "url": raw}

    # "Copy as path" produces a quoted Windows path; a POSIX path is only
    # treated as one when it actually exists, since "/usr/bin is where..." is
    # far more likely to be a sentence than a file.
    unquoted = raw.strip('"').strip()
    if _WINDOWS_PATH.match(unquoted) or unquoted.startswith("\\\\"):
        return {"kind": "file", "filePath": unquoted}
    if unquoted.startswith("/") and "\n" not in unquoted:
        try:
            if Path(unquoted).is_file():
                return {"kind": "file", "filePath": unquoted}
        except OSError:
            pass

    return {"kind": "text", "text": raw}


# --- YouTube, without watching it ---------------------------------------------

def _oembed(url: str) -> dict[str, Any] | None:
    """Title and channel from YouTube's public oEmbed endpoint — no key, no
    scraping. The cheapest possible "what is this" for a video."""
    import httpx

    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": BROWSER_UA}) as client:
            response = client.get("https://www.youtube.com/oembed",
                                  params={"url": url, "format": "json"})
            if response.status_code >= 400:
                return None
            data = response.json()
    except Exception:  # noqa: BLE001
        return None
    return {"title": data.get("title"), "author": data.get("author_name"),
            "thumbnail": data.get("thumbnail_url")}


def _json_array_after(source: str, key: str) -> str | None:
    """The JSON array following `"key":` in a blob of page source.

    A regex genuinely cannot do this — the entries contain nested arrays, so any
    non-greedy match truncates mid-structure. Brackets are counted, respecting
    strings and escapes.
    """
    at = source.find(f'"{key}":')
    if at == -1:
        return None
    start = source.find("[", at)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
        if in_string:
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return None


def _captions(xml: str) -> str:
    from ..webtext import to_text

    lines = [to_text(body).strip()
             for body in re.findall(r"(?s)<text[^>]*>(.*?)</text>", xml)]
    return " ".join(line for line in lines if line)


def fetch_youtube_text(url: str) -> dict[str, Any]:
    """Everything about a video that can be had without watching it.

    **Expect no transcript in practice.** YouTube serves empty caption bodies to
    anything without a session token, and this build is not going to pretend
    otherwise. Title and description come from sturdier sources and do work.
    That gap is exactly why watching properly matters when it is available, and
    why the caller must say which of the two it actually had.
    """
    import httpx

    meta = _oembed(url) or {}
    result: dict[str, Any] = {"ok": True, "title": meta.get("title"),
                              "author": meta.get("author"),
                              "thumbnail": meta.get("thumbnail"),
                              "description": None, "transcript": None}

    markup = ""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True,
                          headers=YOUTUBE_HEADERS) as client:
            response = client.get(url, params={"hl": "en", "gl": "US"})
            if response.status_code < 400:
                markup = response.text
    except Exception:  # noqa: BLE001 — oEmbed alone still says what the video is
        markup = ""

    if not markup:
        if not result["title"]:
            return {"ok": False, "error": "Couldn't reach that video."}
        return result

    described = re.search(r'"shortDescription":"((?:\\.|[^"\\])*)"', markup)
    if described:
        try:
            result["description"] = json.loads(f'"{described.group(1)}"')
        except ValueError:
            result["description"] = described.group(1)

    tracks_json = _json_array_after(markup, "captionTracks")
    if tracks_json:
        try:
            tracks = json.loads(tracks_json.replace("\\u0026", "&"))
            track = (next((t for t in tracks
                           if str(t.get("languageCode", "")).startswith("en")
                           and t.get("kind") != "asr"), None)
                     or next((t for t in tracks
                              if str(t.get("languageCode", "")).startswith("en")), None)
                     or (tracks[0] if tracks else None))
            if track and track.get("baseUrl"):
                with httpx.Client(timeout=20.0, headers=YOUTUBE_HEADERS) as client:
                    captions = client.get(track["baseUrl"])
                if captions.status_code < 400:
                    transcript = _captions(captions.text)
                    if transcript:
                        result["transcript"] = transcript[:TRANSCRIPT_MAX_CHARS]
        except Exception:  # noqa: BLE001 — no captions is the common case
            logger.debug("no usable captions for %s", url, exc_info=True)

    return result


# --- the free glance ----------------------------------------------------------

def identify(source: dict[str, Any]) -> dict[str, Any]:
    """What this content IS. No model call, ever — see this module's docstring.

    For a URL this does fetch the page (a title needs it) and keeps the text as
    `cachedText`, so one fetch serves both "what is this" and the later reading
    rather than paying for two.
    """
    kind = source.get("kind")

    if kind == "youtube":
        meta = _oembed(source["url"]) or {}
        return {"title": meta.get("title") or f"YouTube video ({source.get('videoId')})",
                "kind": "video",
                "detail": f"on {meta['author']}" if meta.get("author") else None,
                "thumbnail": meta.get("thumbnail")}

    if kind == "url":
        article = fetch_article(source["url"])
        if not article.ok:
            return {"title": source["url"], "kind": "article", "detail": None,
                    "unreachable": article.error}
        return {"title": article.title or source["url"], "kind": "article",
                "detail": f"about {max(1, round(len(article.text) / 1000))}k characters",
                "cachedText": article.text}

    if kind == "file":
        path = Path(source["filePath"])
        try:
            size = path.stat().st_size
        except OSError:
            return {"title": path.name, "kind": file_kind(path), "detail": None,
                    "unreachable": "I can't find that file."}
        return {"title": path.name, "kind": file_kind(path), "detail": human_size(size)}

    text = str(source.get("text") or "")
    first_line = text.splitlines()[0].strip() if text.strip() else ""
    title = (f"{first_line[:60]}…" if len(first_line) > 60 else first_line) or "Pasted text"
    return {"title": title, "kind": "text", "detail": f"{len(text)} characters",
            "cachedText": text}


_KIND_WORDS = {"video": "a video", "article": "a web article", "document": "a document",
               "image": "an image", "audio": "an audio file", "text": "some pasted text",
               "unknown": "a file"}


def describe(identity: dict[str, Any]) -> str:
    """Plain-language "what this is", for Jarvis to say straight after the free
    glance — before it has read or watched anything."""
    bits = [_KIND_WORDS.get(identity.get("kind"), "something")]
    if identity.get("title"):
        bits.append(f"— \"{identity['title']}\"")
    if identity.get("detail"):
        bits.append(f"({identity['detail']})")
    return " ".join(bits)


def intake_description(kind: str | None) -> str:
    """How content was actually taken in, said honestly every time, so an answer
    never sounds better-informed than it is."""
    return {
        "video": "I watched the video itself, including what was on screen.",
        "audio": "I listened to the audio.",
        "image": "I looked at the image.",
        "captions": ("I read the captions — I did NOT see the video, so I don't know what "
                     "was shown on screen."),
        "youtube-text": "I only had the title and description — I neither watched nor heard it.",
        "article": "I read the article.",
        "document": "I read the document.",
        "text": "I read the text as given.",
    }.get(kind or "", "I looked at what was provided.")


# --- putting it in front of a model -------------------------------------------

class CannotPrepare(RuntimeError):
    """This model cannot take this source. The message is for the user."""


def prepare_for(entry: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Media for a model that can genuinely watch or see this, plus a cleanup.

    Two routes, cheapest first: INLINE base64 (no upload, works on every
    adapter) for small images and PDFs, and UPLOADED for video, audio, and
    anything over the inline cap. The uploaded route pins the file to ONE
    model's API key, which is why its caller must disable fallback.
    """
    from ..adapters import get_adapter, get_capabilities

    caps = get_capabilities(entry.get("adapter"))
    adapter = get_adapter(entry.get("adapter"))

    if source.get("kind") == "youtube":
        if not caps.get("video"):
            raise CannotPrepare("That model cannot watch video.")
        return {"media": [{"kind": "video", "mimeType": "video/*", "uri": source["url"]}],
                "cleanup": None}

    if source.get("kind") != "file":
        raise CannotPrepare("That kind of source is read as text, not watched.")

    path = Path(source["filePath"])
    kind = file_kind(path)
    mime_type = mime_type_for(path)
    is_pdf = mime_type == "application/pdf"

    if kind == "video" and not caps.get("video"):
        raise CannotPrepare("That model cannot watch video.")
    if kind == "audio" and not caps.get("audio"):
        raise CannotPrepare("That model cannot listen to audio.")
    if kind == "image" and not caps.get("vision"):
        raise CannotPrepare("That model cannot see images.")
    if is_pdf and not caps.get("video"):
        raise CannotPrepare("That model cannot read a PDF directly.")
    if not path.exists():
        raise CannotPrepare("I can't find that file — check the path is right.")

    if kind == "image" or is_pdf:
        try:
            return {"media": inline_attachment(path, mime_type), "cleanup": None}
        except TooBig:
            pass                         # over the cap: fall through to uploading
        except OSError as err:
            raise CannotPrepare("I couldn't read that file.") from err

    upload = getattr(adapter, "upload_file", None)
    if not callable(upload):
        raise CannotPrepare(
            "That file is too big to send directly, and this model's provider has nowhere "
            "to upload it. A Gemini model can handle files this size."
            if kind == "image" or is_pdf else
            f"Taking in {'audio' if kind == 'audio' else 'video'} needs a model that can "
            "accept file uploads — Gemini can.")

    uploaded = upload(entry, str(path), mime_type)
    return {"media": [{"kind": "image" if kind == "image" else "video",
                       "mimeType": uploaded.get("mimeType", mime_type),
                       "uri": uploaded["uri"]}],
            "cleanup": uploaded.get("cleanup")}
