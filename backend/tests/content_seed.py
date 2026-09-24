"""A realistic, busy Content Management — built through the real lifecycle.

Ten niches, each producing its own mix of formats; items spread over every
stage; several platforms per item; schedules across three months in six
timezones (a daylight-saving change included); change requests answered over
one to three revisions; published, failed and retried posts; archive and the
recycle bin. Nothing is written with raw SQL: every row is a state the app
itself reached, so a count that disagrees with a recount is a real bug.

Used by the volume tests, the baseline timings and the screenshot tour.
Deterministic for a given seed.
"""

from __future__ import annotations

import io
import random
import struct
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from content_samples import png

from jarvis.content_manager import files, lifecycle
from jarvis.content_manager.kinds import PLATFORMS, TYPES
from jarvis.content_manager.store import get_item
from jarvis.jscompat import to_iso_z

NICHES: dict[str, dict[str, int]] = {
    # niche: {content type: weight}
    "Nature Sounds": {"video": 6, "audio": 4, "image": 2, "carousel": 1},
    "Fitness": {"video": 6, "carousel": 3, "image": 2, "text_post": 2},
    "Personal Finance": {"article": 4, "carousel": 3, "video": 3, "newsletter": 2, "text_post": 2},
    "Tech News": {"article": 5, "text_post": 4, "video": 3, "newsletter": 2},
    "Home Cooking": {"video": 5, "image": 3, "carousel": 3, "article": 2},
    "Travel": {"video": 4, "image": 4, "carousel": 3, "article": 2},
    "Mindfulness": {"audio": 5, "text_post": 3, "image": 2, "video": 2},
    "Gaming": {"video": 7, "image": 2, "text_post": 2},
    "Real Estate": {"flyer": 4, "carousel": 3, "video": 3, "article": 2},
    "Parenting": {"article": 3, "carousel": 3, "text_post": 3, "video": 2, "newsletter": 1},
}

ZONES = ["Europe/London", "America/New_York", "America/Los_Angeles", "Africa/Lagos",
         "Asia/Kolkata", "Australia/Sydney"]

_SUBJECTS = {
    "Nature Sounds": ["Rain on a Tin Roof", "Forest Birdsong at Dawn", "Ocean Waves at Night",
                      "Thunderstorm in the Mountains", "Crackling Campfire", "Creek in Spring"],
    "Fitness": ["3 Squat Mistakes", "10-Minute Core Burn", "Beginner Pull-Up Plan",
                "Mobility Before Lifting", "Home Dumbbell Workout", "Protein Myths"],
    "Personal Finance": ["Emergency Fund Basics", "Index Funds Explained", "Budgeting on a Low Income",
                         "Paying Off Debt Faster", "Retirement at 30", "Side Income Ideas"],
    "Tech News": ["This Week in AI", "New Phone Launch Recap", "Chip Shortage Update",
                  "Open-Source Model Release", "Browser Privacy Changes", "Laptop Buying Guide"],
    "Home Cooking": ["One-Pot Jollof Rice", "15-Minute Pasta", "Sourdough for Beginners",
                     "Meal Prep Sunday", "Crispy Plantain", "Weeknight Stir-Fry"],
    "Travel": ["48 Hours in Lisbon", "Budget Bali Guide", "Hidden Beaches of Ghana",
               "Packing Light", "Night Markets of Taipei", "Train Across Europe"],
    "Mindfulness": ["5-Minute Breathing", "Body Scan for Sleep", "Morning Gratitude",
                    "Letting Go of Stress", "Walking Meditation", "Focus Reset"],
    "Gaming": ["Speedrun Highlights", "Top 5 Indie Games", "Patch Notes Breakdown",
               "Boss Fight Guide", "Controller vs Keyboard", "Retro Console Tour"],
    "Real Estate": ["Open House Saturday", "3-Bed Family Home", "First-Time Buyer Tips",
                    "Market Update", "Downtown Loft Tour", "Renting vs Buying"],
    "Parenting": ["Toddler Sleep Tips", "Screen Time Rules", "Lunchbox Ideas",
                  "Talking About Feelings", "Homework Without Tears", "Family Game Night"],
}

_LONG = ("Here is the full story, told properly. " * 40).strip()


def wav(seconds: float = 0.4, rate: int = 8000) -> bytes:
    """A genuine, playable WAV (a soft tone), standard library only."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(3000 * ((i // 20) % 2 * 2 - 1)))
                          for i in range(int(seconds * rate)))
        w.writeframes(frames)
    return out.getvalue()


#: Not a playable video — the backend never decodes media. The screenshot tour
#: passes real WebMs recorded in Chromium instead (`media={"webm": ...}`).
FAKE_WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


@dataclass
class Seeded:
    items: list[str] = field(default_factory=list)
    niches: list[str] = field(default_factory=list)
    plan: dict[str, str] = field(default_factory=dict)  # item id -> what the seeder aimed for


def _utc(local: datetime, zone: str) -> str:
    return to_iso_z(local.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc))


class _Maker:
    def __init__(self, rnd: random.Random, media: dict[str, bytes]):
        self.rnd = rnd
        self.webm = media.get("webm") or FAKE_WEBM
        self.webm_vertical = media.get("webm_vertical") or self.webm
        self.audio = media.get("wav") or wav()

    def _save(self, name: str, data: bytes) -> str:
        return files.save_stream(io.BytesIO(data), name)["fileId"]

    def image(self, vertical: bool = False) -> bytes:
        rgb = tuple(self.rnd.randint(30, 220) for _ in range(3))
        return png(36, 64, rgb) if vertical else png(64, 36, rgb)

    def media(self, content_type: str, vertical: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if content_type == "video":
            out.append({"fileId": self._save("clip.webm", self.webm_vertical if vertical else self.webm),
                        "role": "primary"})
            out.append({"fileId": self._save("thumb.png", self.image(vertical)), "role": "thumbnail"})
        elif content_type in ("image", "flyer"):
            out.append({"fileId": self._save("image.png", self.image(vertical)), "role": "primary"})
        elif content_type == "carousel":
            for i in range(self.rnd.randint(3, 10)):
                out.append({"fileId": self._save(f"slide{i + 1}.png", self.image(True)), "role": "slide",
                            "order": i})
            if self.rnd.random() < 0.5:
                out.append({"fileId": self._save("cover.png", self.image(True)), "role": "cover"})
        elif content_type == "audio":
            out.append({"fileId": self._save("episode.wav", self.audio), "role": "primary"})
            out.append({"fileId": self._save("cover.png", self.image()), "role": "cover"})
        return out

    def fields(self, content_type: str, niche: str, title: str) -> dict[str, Any]:
        tag = "#" + niche.replace(" ", "").lower()
        allowed = TYPES[content_type]["fields"]
        body = _LONG if self.rnd.random() < 0.15 else f"{title}. A short, useful note for the {niche} crowd."
        values = {
            "title": title if self.rnd.random() > 0.1 else title + " — the complete, no-nonsense guide for "
                                                               "people who have tried everything else",
            "subject": f"{niche} weekly: {title}",
            "body": body,
            "description": f"Everything about {title.lower()}.",
            "caption": f"{title} 👇 save this for later",
            "hashtags": [tag, "#fyp", f"#{content_type}"][: self.rnd.randint(1, 3)],
            "tags": [niche.lower(), content_type],
        }
        return {k: v for k, v in values.items() if k in allowed}


def seed(count: int = 1500, *, rng_seed: int = 7, media: dict[str, bytes] | None = None,
         now: datetime | None = None) -> Seeded:
    """Build `count` items through the real lifecycle. Returns what was made."""
    rnd = random.Random(rng_seed)
    maker = _Maker(rnd, media or {})
    now = now or datetime.now(timezone.utc)
    out = Seeded(niches=list(NICHES))
    numbers: dict[str, int] = {}

    for index in range(count):
        niche = list(NICHES)[index % len(NICHES)]
        weights = NICHES[niche]
        content_type = rnd.choices(list(weights), weights=list(weights.values()))[0]
        subject = rnd.choice(_SUBJECTS[niche])
        numbers[subject] = numbers.get(subject, 0) + 1
        name = f"{subject} #{numbers[subject]:03d}"
        vertical = content_type == "video" and rnd.random() < 0.55
        accepts = [p for p, spec in PLATFORMS.items() if content_type in spec["accepts"]]
        chosen = rnd.sample(accepts, k=min(len(accepts), rnd.randint(1, 5)))
        platforms = []
        for p in chosen:
            destination = ""
            if p == "youtube" and vertical:
                destination = "Shorts"
            elif p == "instagram" and content_type == "video":
                destination = "Reels"
            platforms.append({"platform": p, "destination": destination})

        item = lifecycle.submit(
            name=name, content_type=content_type, niche=niche,
            fields=maker.fields(content_type, niche, subject),
            media=maker.media(content_type, vertical),
            findings=([{"level": rnd.choice(["note", "warning", "problem"]), "text": "Hook lands late"}]
                      if rnd.random() < 0.3 else None),
            producer=rnd.choice(["ClipBot", "Writer", "Jarvis", "you", "DesignBot"]),
            platforms=platforms)
        item_id = item["id"]
        out.items.append(item_id)

        roll = rnd.random()
        # Some go through one to three rounds of changes first.
        rounds = rnd.choice([0, 0, 0, 1, 1, 2, 3]) if roll > 0.12 else 0
        for r in range(rounds):
            lifecycle.request_changes(item_id, what=f"Tighten the opening (round {r + 1})",
                                      why="Viewers drop off early")
            if roll < 0.20 and r == rounds - 1:
                out.plan[item_id] = "changes_requested"
                break
            patch = {k: v for k, v in maker.fields(content_type, niche, subject + " (v2)").items()
                     if k in ("title", "caption", "body", "subject")}
            if not patch:
                patch = {TYPES[content_type]["fields"][0]: f"{subject} revised"}
            lifecycle.submit_revision(item_id, fields=patch, note=f"Round {r + 1} done", by="ClipBot")
        if out.plan.get(item_id) == "changes_requested":
            continue
        if roll < 0.12:
            out.plan[item_id] = "review"
            continue

        lifecycle.approve(item_id)
        placements = get_item(item_id)["placements"]
        outcome = rnd.random()
        for p in placements:
            fate = rnd.random()
            if outcome < 0.25:
                continue  # Ready to Post, nothing scheduled
            if fate < 0.45:
                zone = rnd.choice(ZONES)
                day = rnd.randint(1, 90)
                local = (now + timedelta(days=day)).astimezone(ZoneInfo(zone)).replace(
                    hour=rnd.choice([7, 9, 12, 18, 21]), minute=rnd.choice([0, 15, 30]), second=0,
                    microsecond=0, tzinfo=None)
                lifecycle.schedule(p["id"], scheduled_at=_utc(local, zone), timezone_name=zone)
            elif fate < 0.9:
                lifecycle.post_now(p["id"])
                lifecycle.claim(p["id"], by="PostBot")
                if rnd.random() < 0.12:
                    lifecycle.report_result(p["id"], ok=False, by="PostBot",
                                            error=rnd.choice(["The video is too long for this platform.",
                                                              "The login for this account expired.",
                                                              "Rate limited — try again later."]))
                else:
                    lifecycle.report_result(p["id"], ok=True, by="PostBot",
                                            url=f"https://{p['platform']}.example/{item_id}/{p['id']}")
                    # Most published posts get numbers reported, some more than once.
                    if rnd.random() < 0.65:
                        views = rnd.randint(80, 250_000)
                        for day in range(rnd.randint(1, 3)):
                            grown = int(views * (1 + day * 0.6))
                            values = {"views": grown, "likes": int(grown * rnd.uniform(0.01, 0.09)),
                                      "comments": int(grown * rnd.uniform(0.001, 0.01)),
                                      "shares": int(grown * rnd.uniform(0.001, 0.02))}
                            if rnd.random() < 0.5:
                                values["saves"] = int(grown * rnd.uniform(0.002, 0.03))
                                values["reach"] = int(grown * rnd.uniform(0.8, 1.6))
                            if content_type in ("video", "audio"):
                                values["watchTimeSeconds"] = grown * rnd.randint(5, 90)
                            lifecycle.record_metrics(p["id"], metrics=values, by=rnd.choice(["PostBot", "you"]),
                                                     captured_at=_utc((now - timedelta(days=3 - day)).replace(
                                                         tzinfo=None), "UTC"))
            # else: left as a draft on this platform
        final = rnd.random()
        if final < 0.07:
            lifecycle.archive(item_id)
            out.plan[item_id] = "archived"
        elif final < 0.12:
            lifecycle.delete(item_id)
            out.plan[item_id] = "bin"
        else:
            out.plan[item_id] = get_item(item_id)["stage"]

    # A handful in Review and the bin straight away, so every view has something.
    for extra in range(min(5, count // 50)):
        item = lifecycle.submit(name=f"Quick draft {extra + 1}", content_type="text_post", niche="Tech News",
                                fields={"body": "Draft."}, producer="Writer")
        lifecycle.delete(item["id"])
        out.items.append(item["id"])
        out.plan[item["id"]] = "bin"
    return out


# --- niches as folders: many pieces of every kind per niche -------------------------------

#: Ten niches as a creator would really run them: each one a folder holding 20+
#: videos plus carousels, images, blogs and the rest — every piece its own item.
#: One long name and one with "&" and "/", on purpose.
NICHE_FOLDERS: dict[str, list[str]] = {
    "Psychology": ["Why We Procrastinate", "The Spotlight Effect", "Dopamine and Habits", "Imposter Syndrome",
                   "Decision Fatigue", "The Halo Effect", "Cognitive Dissonance", "Attachment Styles"],
    "Fitness": ["3 Squat Mistakes", "10-Minute Core Burn", "Beginner Pull-Up Plan", "Mobility Before Lifting",
                "Home Dumbbell Workout", "Protein Myths", "Zone 2 Cardio"],
    "Personal Finance": ["Emergency Fund Basics", "Index Funds Explained", "Budgeting on a Low Income",
                         "Paying Off Debt Faster", "Side Income Ideas", "Compound Interest"],
    "Cooking & Baking": ["One-Pot Jollof Rice", "15-Minute Pasta", "Sourdough for Beginners", "Meal Prep Sunday",
                         "Crispy Plantain", "Lemon Drizzle Cake"],
    "Travel": ["48 Hours in Lisbon", "Budget Bali Guide", "Hidden Beaches of Ghana", "Packing Light",
               "Night Markets of Taipei", "Train Across Europe"],
    "Tech Reviews": ["Budget Phone Showdown", "Best Laptop for Students", "Noise-Cancelling Earbuds",
                     "Smartwatch After 6 Months", "Mechanical Keyboards"],
    "Parenting": ["Toddler Sleep Tips", "Screen Time Rules", "Lunchbox Ideas", "Talking About Feelings",
                  "Homework Without Tears"],
    "Mindfulness": ["5-Minute Breathing", "Body Scan for Sleep", "Morning Gratitude", "Walking Meditation",
                    "Letting Go of Stress"],
    "History / Ancient World": ["The Fall of Rome", "Life in Ancient Egypt", "The Silk Road",
                                "Mansa Musa's Fortune", "The Library of Alexandria"],
    "Productivity, Habits & Deep Work for Busy Creators": ["Time Blocking", "The 2-Minute Rule",
                                                           "Batching Content", "Weekly Review", "Digital Minimalism"],
}

#: Per niche, how many of each kind (low, high).
NICHE_MIX: dict[str, tuple[int, int]] = {
    "video": (23, 28), "carousel": (5, 8), "image": (5, 8), "article": (3, 5), "text_post": (2, 3),
    "flyer": (1, 2), "audio": (1, 2), "newsletter": (1, 2),
}


class _Clock:
    """The lifecycle's clock, set to a moment in the past three months while one
    item is made, so created, published and archived dates really spread out.
    Everything else is the real code path."""

    def __init__(self):
        self.at = datetime.now(timezone.utc)

    def __call__(self) -> str:
        self.at += timedelta(seconds=37)
        return to_iso_z(min(self.at, datetime.now(timezone.utc)))


def seed_niches(*, scale: int = 1, rng_seed: int = 33, media: dict[str, bytes] | None = None,
                loose: int = 30) -> Seeded:
    """About 450 items per `scale`: ten niche folders of every kind, `loose` items
    with no niche, one empty niche, and a few handed in by an agent under another
    spelling of a niche ("psychology"). Every stage, several platforms each."""
    from jarvis.content_manager import files as files_module
    from jarvis.content_manager import lifecycle as lifecycle_module

    rnd = random.Random(rng_seed)
    maker = _Maker(rnd, media or {})
    out = Seeded(niches=list(NICHE_FOLDERS))
    clock = _Clock()
    real = (lifecycle_module.now_iso, files_module.now_iso)
    lifecycle_module.now_iso = clock
    files_module.now_iso = clock
    numbers: dict[str, int] = {}
    try:
        # The person made the folders first; "Astronomy" was made and never filled.
        for niche in [*NICHE_FOLDERS, "Astronomy"]:
            lifecycle.create_niche(niche)
        work: list[tuple[str, str, str]] = []
        for niche, subjects in NICHE_FOLDERS.items():
            for content_type, (low, high) in NICHE_MIX.items():
                for _ in range(rnd.randint(low, high) * scale):
                    work.append((niche, content_type, rnd.choice(subjects)))
        for n in range(loose * scale):
            work.append(("", rnd.choice(["video", "video", "image", "carousel", "text_post"]),
                         rnd.choice(["Untitled clip", "Random idea", "Test upload", "Behind the scenes"])))
        rnd.shuffle(work)
        for niche, content_type, subject in work:
            numbers[subject] = numbers.get(subject, 0) + 1
            name = f"{subject} #{numbers[subject]:03d}"
            clock.at = datetime.now(timezone.utc) - timedelta(days=rnd.uniform(1, 92))
            # A few from an agent that spells the niche its own way.
            spelled = niche.lower() if niche == "Psychology" and rnd.random() < 0.1 else niche
            out.items.append(_drive(rnd, maker, out, name, content_type, spelled, subject))
    finally:
        lifecycle_module.now_iso, files_module.now_iso = real
    return out


def _drive(rnd: random.Random, maker: _Maker, out: Seeded, name: str, content_type: str, niche: str,
           subject: str) -> str:
    """One piece, handed in and taken as far through the pipeline as it goes."""
    vertical = content_type == "video" and rnd.random() < 0.6
    accepts = [p for p, spec in PLATFORMS.items() if content_type in spec["accepts"]]
    chosen = rnd.sample(accepts, k=min(len(accepts), rnd.randint(1, 4)))
    platforms = [{"platform": p, "destination": "Shorts" if p == "youtube" and vertical
                  else "Reels" if p == "instagram" and content_type == "video" else ""} for p in chosen]
    item = lifecycle.submit(
        name=name, content_type=content_type, niche=niche,
        fields=maker.fields(content_type, niche or "General", subject),
        media=maker.media(content_type, vertical),
        producer=rnd.choice(["ClipBot", "Writer", "Jarvis", "you", "DesignBot"]), platforms=platforms)
    item_id = item["id"]
    roll = rnd.random()
    if roll < 0.14:
        out.plan[item_id] = "review"
        return item_id
    if roll < 0.22:
        lifecycle.request_changes(item_id, what="Tighten the opening", why="Viewers drop off early",
                                  assignee=rnd.choice(["agent", "agent", "jarvis"]))
        if rnd.random() < 0.6:
            out.plan[item_id] = "changes_requested"
            return item_id
        field_name = TYPES[content_type]["fields"][0]
        lifecycle.submit_revision(item_id, fields={field_name: f"{subject} (revised)"}, note="Done", by="ClipBot")
        if rnd.random() < 0.5:
            out.plan[item_id] = "review"
            return item_id
    lifecycle.approve(item_id)
    outcome = rnd.random()
    for p in get_item(item_id)["placements"]:
        fate = rnd.random()
        if outcome < 0.22:
            continue                                   # Ready to Post, nothing done yet
        if fate < 0.35:
            zone = rnd.choice(ZONES)
            local = (datetime.now(timezone.utc) + timedelta(days=rnd.randint(1, 60))).astimezone(
                ZoneInfo(zone)).replace(hour=rnd.choice([7, 9, 12, 18, 21]), minute=rnd.choice([0, 30]),
                                        second=0, microsecond=0, tzinfo=None)
            lifecycle.schedule(p["id"], scheduled_at=_utc(local, zone), timezone_name=zone)
        elif fate < 0.85:
            lifecycle.post_now(p["id"])
            if rnd.random() < 0.05:
                continue                               # still waiting for the publisher
            lifecycle.claim(p["id"], by="PostBot")
            if rnd.random() < 0.08:
                lifecycle.report_result(p["id"], ok=False, by="PostBot", error="The login for this account expired.")
                continue
            lifecycle.report_result(p["id"], ok=True, by="PostBot",
                                    url=f"https://{p['platform']}.example/{item_id}/{p['id']}")
            if rnd.random() < 0.6:
                views = rnd.randint(80, 250_000)
                lifecycle.record_metrics(p["id"], metrics={"views": views, "likes": int(views * 0.04),
                                                            "comments": int(views * 0.004)}, by="PostBot")
    final = rnd.random()
    if final < 0.06:
        lifecycle.archive(item_id)
        out.plan[item_id] = "archived"
    elif final < 0.10:
        lifecycle.delete(item_id)
        out.plan[item_id] = "bin"
    else:
        out.plan[item_id] = get_item(item_id)["stage"]
    return item_id
