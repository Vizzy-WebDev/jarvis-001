"""What kinds of content exist, and what each platform takes.

The one place to add a content type or a platform. The UI and agents read this
same registry from `GET /api/content-meta`, so a new entry shows up everywhere
with no screen changes: which supporting fields and assets an item shows is
computed from here, never hardcoded in a component.

Supporting fields are NOT content. A video's title, caption and hashtags belong
to the video; they are listed here as the fields that type carries, and a
platform lists the fields it actually uses. A platform version shows the
intersection.
"""

from __future__ import annotations

from typing import Any

#: Every supporting field any type may carry. `list` fields hold string lists.
FIELDS: dict[str, dict[str, Any]] = {
    "title": {"label": "Title", "kind": "text"},
    "subject": {"label": "Subject line", "kind": "text"},
    "body": {"label": "Text", "kind": "longtext"},
    "description": {"label": "Description", "kind": "longtext"},
    "caption": {"label": "Caption", "kind": "longtext"},
    "hashtags": {"label": "Hashtags", "kind": "list"},
    "tags": {"label": "Tags", "kind": "list"},
}

#: Media roles. `primary` / `slide` are the content itself; the rest are assets.
ASSETS: dict[str, str] = {"thumbnail": "Thumbnail", "cover": "Cover", "attachment": "Attachment"}

#: `render` decides how the actual content is shown:
#:   video | image | slides | text | audio
#: `media` is which media role holds the content itself (None: the content is
#: the `body` field).
TYPES: dict[str, dict[str, Any]] = {
    "video": {"label": "Video", "render": "video", "media": "primary",
              "fields": ["title", "description", "caption", "hashtags", "tags"],
              "assets": ["thumbnail"]},
    "image": {"label": "Image", "render": "image", "media": "primary",
              "fields": ["title", "caption", "hashtags", "tags"], "assets": []},
    "carousel": {"label": "Carousel", "render": "slides", "media": "slide",
                 "fields": ["caption", "hashtags", "tags"], "assets": ["cover"]},
    "flyer": {"label": "Flyer", "render": "image", "media": "primary",
              "fields": ["title", "caption", "hashtags"], "assets": ["attachment"]},
    "text_post": {"label": "Text Post", "render": "text", "media": None,
                  "fields": ["body", "hashtags", "tags"], "assets": ["attachment"]},
    "article": {"label": "Blog / Article", "render": "text", "media": None,
                "fields": ["title", "body", "description", "tags"], "assets": ["cover"]},
    "newsletter": {"label": "Newsletter", "render": "text", "media": None,
                   "fields": ["subject", "body"], "assets": ["attachment"]},
    "audio": {"label": "Audio / Podcast", "render": "audio", "media": "primary",
              "fields": ["title", "description", "tags"], "assets": ["cover"]},
}

#: `fields` a platform uses; `accepts` the content types it can take.
PLATFORMS: dict[str, dict[str, Any]] = {
    "youtube": {"label": "YouTube", "fields": ["title", "description", "tags"],
                "accepts": ["video"]},
    "tiktok": {"label": "TikTok", "fields": ["caption", "hashtags"],
               "accepts": ["video", "carousel", "image"]},
    "instagram": {"label": "Instagram", "fields": ["caption", "hashtags"],
                  "accepts": ["video", "image", "carousel", "flyer"]},
    "facebook": {"label": "Facebook", "fields": ["title", "caption", "body", "hashtags"],
                 "accepts": ["video", "image", "carousel", "flyer", "text_post", "article"]},
    "linkedin": {"label": "LinkedIn", "fields": ["title", "caption", "body", "hashtags"],
                 "accepts": ["video", "image", "carousel", "flyer", "text_post", "article"]},
    "x": {"label": "X", "fields": ["caption", "body", "hashtags"],
          "accepts": ["video", "image", "carousel", "flyer", "text_post"]},
    "pinterest": {"label": "Pinterest", "fields": ["title", "description", "tags"],
                  "accepts": ["image", "flyer", "video"]},
    "blog": {"label": "Blog / Website", "fields": ["title", "body", "description", "tags"],
             "accepts": ["article"]},
    "newsletter": {"label": "Newsletter", "fields": ["subject", "body"],
                   "accepts": ["newsletter"]},
    "podcast": {"label": "Podcast host", "fields": ["title", "description", "tags"],
                "accepts": ["audio"]},
}

#: The lifecycle stages, in order, with the words the UI shows.
STAGES: list[dict[str, str]] = [
    {"id": "review", "label": "Review"},
    {"id": "changes_requested", "label": "Changes Requested"},
    {"id": "approved", "label": "Ready to Post"},
    {"id": "scheduling", "label": "Scheduling"},
    {"id": "published", "label": "Published"},
    {"id": "archived", "label": "Archived"},
]
STAGE_IDS = [s["id"] for s in STAGES]


def type_label(content_type: str) -> str:
    return (TYPES.get(content_type) or {}).get("label") or content_type


def platform_label(platform: str) -> str:
    return (PLATFORMS.get(platform) or {}).get("label") or platform


def platform_fields(content_type: str, platform: str) -> list[str]:
    """The fields a platform version of this item shows: the type's own fields
    that the platform also uses, in the type's order."""
    own = (TYPES.get(content_type) or {}).get("fields") or []
    uses = set((PLATFORMS.get(platform) or {}).get("fields") or [])
    return [f for f in own if f in uses]


def meta() -> dict[str, Any]:
    return {"fields": FIELDS, "assets": ASSETS, "types": TYPES,
            "platforms": PLATFORMS, "stages": STAGES}
