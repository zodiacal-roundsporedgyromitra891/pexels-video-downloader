#!/usr/bin/env python3
"""
Pexels Video Downloader — download categorized stock videos via the Pexels API.

Setup:
  1. cp .env.example .env   # set PEXELS_API_KEY
  2. pip install -r requirements.txt
  3. Edit config.yaml
  4. python download_pexels.py

Examples:
  python download_pexels.py
  python download_pexels.py --dry-run
  python download_pexels.py --category nature-calm
  python download_pexels.py --per-category 5
  python download_pexels.py --config /path/to/config.yaml
  python download_pexels.py --regen-manifest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"
LIBRARY_MD = "LIBRARY.md"
API_BASE = "https://api.pexels.com/v1/videos/search"
VALID_ORIENTATIONS = {"portrait", "landscape", "square"}
VALID_SIZES = {"", "large", "medium", "small"}
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Only download from known Pexels / Vimeo CDN hosts (SSRF hardening)
ALLOWED_DOWNLOAD_HOST_SUFFIXES = (
    "pexels.com",
    "vimeo.com",
    "vimeocdn.com",
)


@dataclass
class Settings:
    per_category: int = 10
    orientation: str = "portrait"
    size: str = ""
    min_duration: int = 4
    max_duration: int = 15
    preferred_width: int = 1080
    preferred_height: int = 1920
    request_delay_seconds: float = 1.2
    output_dir: str = "downloads"
    state_file: str = "download_state.json"


@dataclass
class Category:
    folder: str
    description: str
    queries: list[str]
    count: int | None = None


@dataclass
class DownloadState:
    used_ids: set[int] = field(default_factory=set)
    clips: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "DownloadState":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            used_ids=set(data.get("used_ids", [])),
            clips=data.get("clips", []),
        )

    def save(self, path: Path) -> None:
        payload = {
            "used_ids": sorted(self.used_ids),
            "clips": self.clips,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def slugify(text: str, max_len: int = 48) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len].rstrip("-") or "clip"


def sanitize_folder_name(name: str) -> str:
    """Reject path traversal / odd folder names from config."""
    cleaned = name.strip().replace("\\", "/").rstrip("/")
    if "/" in cleaned or cleaned in {".", ".."} or not FOLDER_RE.match(cleaned):
        raise ValueError(
            f"Invalid folder name {name!r}. "
            "Use letters, numbers, dots, underscores, hyphens only "
            "(no paths like ../)."
        )
    return cleaned


def resolve_under_root(relative: str, *, label: str) -> Path:
    """Resolve a relative path and ensure it stays inside the project root."""
    rel = relative.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        raise ValueError(f"{label} must be a relative path inside the project")
    if ".." in Path(rel).parts:
        raise ValueError(f"{label} must not contain '..'")
    resolved = (ROOT / rel).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the project directory") from exc
    return resolved


def is_allowed_download_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_DOWNLOAD_HOST_SUFFIXES)


def load_config(path: Path) -> tuple[Settings, list[Category]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = raw.get("settings") or {}

    orientation = str(s.get("orientation", "portrait")).strip().lower()
    if orientation not in VALID_ORIENTATIONS:
        raise ValueError(
            f"Invalid orientation {orientation!r}. "
            f"Use one of: {', '.join(sorted(VALID_ORIENTATIONS))}"
        )

    size = str(s.get("size") or "").strip().lower()
    if size not in VALID_SIZES:
        raise ValueError(
            f"Invalid size {size!r}. "
            f"Use one of: large, medium, small, or leave empty."
        )

    settings = Settings(
        per_category=int(s.get("per_category", 10)),
        orientation=orientation,
        size=size,
        min_duration=int(s.get("min_duration", 4)),
        max_duration=int(s.get("max_duration", 15)),
        preferred_width=int(s.get("preferred_width", 1080)),
        preferred_height=int(s.get("preferred_height", 1920)),
        request_delay_seconds=float(s.get("request_delay_seconds", 1.2)),
        output_dir=str(s.get("output_dir", "downloads")),
        state_file=str(s.get("state_file", "download_state.json")),
    )

    if settings.min_duration > settings.max_duration:
        raise ValueError("min_duration must be <= max_duration")
    if settings.per_category < 1:
        raise ValueError("per_category must be >= 1")
    if settings.request_delay_seconds < 0:
        raise ValueError("request_delay_seconds must be >= 0")

    # Validate paths early (no traversal outside project)
    resolve_under_root(settings.output_dir, label="output_dir")
    resolve_under_root(settings.state_file, label="state_file")

    categories_raw = raw.get("categories") or []
    if not categories_raw:
        raise ValueError("config.yaml must define at least one category")

    categories: list[Category] = []
    for i, c in enumerate(categories_raw):
        if not c.get("folder"):
            raise ValueError(f"categories[{i}] is missing 'folder'")
        folder = sanitize_folder_name(str(c["folder"]))
        queries = list(c.get("queries") or [])
        if not queries:
            raise ValueError(f"categories[{i}] ({folder}) needs at least one query")
        count = c.get("count")
        if count is not None and int(count) < 1:
            raise ValueError(f"categories[{i}] ({folder}) count must be >= 1")
        categories.append(
            Category(
                folder=folder,
                description=str(c.get("description") or "").strip(),
                queries=[str(q).strip() for q in queries if str(q).strip()],
                count=int(count) if count is not None else None,
            )
        )

    return settings, categories


class PexelsClient:
    def __init__(self, api_key: str, delay: float) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": api_key})
        self.delay = delay
        self._last_request = 0.0
        self.remaining: int | None = None

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def search(
        self,
        query: str,
        *,
        orientation: str,
        size: str = "",
        per_page: int = 40,
        page: int = 1,
    ) -> dict[str, Any]:
        self._throttle()
        params: dict[str, Any] = {
            "query": query,
            "orientation": orientation,
            "per_page": per_page,
            "page": page,
        }
        if size:
            params["size"] = size

        url = f"{API_BASE}?{urlencode(params)}"
        resp = self.session.get(url, timeout=60)
        self._last_request = time.monotonic()

        if resp.status_code == 401:
            raise SystemExit(
                "Unauthorized (401). Check PEXELS_API_KEY in your .env file."
            )
        if resp.status_code == 429:
            reset = resp.headers.get("X-Ratelimit-Reset")
            wait = 60
            if reset:
                wait = max(5, int(reset) - int(time.time()))
            print(f"  Rate limited. Waiting {wait}s…")
            time.sleep(wait)
            return self.search(
                query,
                orientation=orientation,
                size=size,
                per_page=per_page,
                page=page,
            )

        resp.raise_for_status()
        rem = resp.headers.get("X-Ratelimit-Remaining")
        if rem is not None:
            self.remaining = int(rem)
            if self.remaining < 20:
                print(f"  Warning: only {self.remaining} API requests remaining this hour.")
        return resp.json()

    def download_file(self, url: str, dest: Path) -> None:
        if not is_allowed_download_url(url):
            raise ValueError(
                "Refusing download from unexpected host "
                "(expected https://…pexels.com / vimeo.com CDN)."
            )
        # Keep destination inside the project tree
        dest = dest.resolve()
        try:
            dest.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError("Refusing to write outside the project directory") from exc

        self._throttle()
        with self.session.get(url, stream=True, timeout=180, allow_redirects=True) as resp:
            self._last_request = time.monotonic()
            # Re-check final URL after redirects
            if not is_allowed_download_url(resp.url):
                raise ValueError(
                    f"Refusing redirected download host: {urlparse(resp.url).hostname}"
                )
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
            tmp.replace(dest)


def matches_orientation(video: dict[str, Any], orientation: str) -> bool:
    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    if w <= 0 or h <= 0:
        return False
    if orientation == "portrait":
        return h > w
    if orientation == "landscape":
        return w > h
    # square — allow near-square as well
    ratio = w / h
    return 0.9 <= ratio <= 1.1


def duration_ok(video: dict[str, Any], settings: Settings) -> bool:
    d = int(video.get("duration", 0))
    return settings.min_duration <= d <= settings.max_duration


def file_matches_orientation(f: dict[str, Any], orientation: str) -> bool:
    w, h = int(f.get("width") or 0), int(f.get("height") or 0)
    if w <= 0 or h <= 0:
        return False
    if orientation == "portrait":
        return h > w
    if orientation == "landscape":
        return w > h
    ratio = w / h
    return 0.9 <= ratio <= 1.1


def pick_best_file(video: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    """Prefer MP4 closest to preferred resolution, matching orientation."""
    files = [
        f
        for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4"
        and f.get("link")
        and file_matches_orientation(f, settings.orientation)
    ]
    if not files:
        files = [
            f
            for f in video.get("video_files", [])
            if f.get("file_type") == "video/mp4" and f.get("link")
        ]
    if not files:
        return None

    pw, ph = settings.preferred_width, settings.preferred_height

    def score(f: dict[str, Any]) -> tuple:
        w, h = int(f.get("width") or 0), int(f.get("height") or 0)
        dist = abs(w - pw) + abs(h - ph)
        quality_rank = 0 if f.get("quality") == "hd" else 1
        return (quality_rank, dist, -w * h)

    return sorted(files, key=score)[0]


def description_from_query(query: str) -> str:
    return query.strip().capitalize()


def build_filename(index: int, query: str, video_id: int) -> str:
    return f"{index:02d}-{slugify(query)}-{video_id}.mp4"


def write_library_md(
    out_dir: Path,
    categories: list[Category],
    clips: list[dict[str, Any]],
    settings: Settings,
) -> None:
    by_cat: dict[str, list[dict[str, Any]]] = {c.folder: [] for c in categories}
    cat_meta = {c.folder: c for c in categories}

    for clip in clips:
        folder = clip["category"]
        by_cat.setdefault(folder, []).append(clip)

    lines = [
        "# Video Library",
        "",
        "Auto-generated catalog of clips downloaded from Pexels.",
        f"Orientation: `{settings.orientation}` · "
        f"duration filter: `{settings.min_duration}–{settings.max_duration}s`.",
        "",
        "Durations come from the API; re-probe with ffprobe if you trim files.",
        "",
    ]

    for folder, items in by_cat.items():
        if not items:
            continue
        items = sorted(items, key=lambda c: c["index"])
        meta = cat_meta.get(folder)
        lines.append(f"## {folder}")
        if meta and meta.description:
            lines.append("")
            lines.append(meta.description)
        lines.append("")
        for clip in items:
            lines.append(
                f"{clip['index']}. `{clip['filename']}` — {clip['description']} "
                f"({clip['duration']}s, {clip['width']}x{clip['height']}) "
                f"— [Pexels]({clip['pexels_url']}) by {clip['photographer']}"
            )
        lines.append("")

    (out_dir / LIBRARY_MD).write_text("\n".join(lines), encoding="utf-8")


def collect_candidates(
    client: PexelsClient,
    category: Category,
    settings: Settings,
    used_ids: set[int],
    need: int,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen_this_pass: set[int] = set()

    for query in category.queries:
        if len(found) >= need:
            break
        print(f"  Searching: {query!r}")
        data = client.search(
            query,
            orientation=settings.orientation,
            size=settings.size,
            per_page=min(40, max(need * 3, 15)),
            page=1,
        )
        for page_data in (data,):
            for video in page_data.get("videos") or []:
                vid = int(video["id"])
                if vid in used_ids or vid in seen_this_pass:
                    continue
                if not matches_orientation(video, settings.orientation):
                    continue
                if not duration_ok(video, settings):
                    continue
                best = pick_best_file(video, settings)
                if not best:
                    continue
                seen_this_pass.add(vid)
                found.append((query, video, best))
                if len(found) >= need:
                    break

        if len(found) < need and data.get("next_page"):
            data2 = client.search(
                query,
                orientation=settings.orientation,
                size=settings.size,
                per_page=40,
                page=2,
            )
            for video in data2.get("videos") or []:
                vid = int(video["id"])
                if vid in used_ids or vid in seen_this_pass:
                    continue
                if not matches_orientation(video, settings.orientation):
                    continue
                if not duration_ok(video, settings):
                    continue
                best = pick_best_file(video, settings)
                if not best:
                    continue
                seen_this_pass.add(vid)
                found.append((query, video, best))
                if len(found) >= need:
                    break

    return found[:need]


def already_have_in_folder(folder: Path) -> int:
    if not folder.exists():
        return 0
    return len([p for p in folder.glob("*.mp4") if p.is_file()])


def next_index(folder: Path) -> int:
    existing = []
    for p in folder.glob("*.mp4"):
        m = re.match(r"^(\d+)-", p.name)
        if m:
            existing.append(int(m.group(1)))
    return (max(existing) + 1) if existing else 1


def category_target(category: Category, default: int) -> int:
    return category.count if category.count is not None else default


def download_category(
    client: PexelsClient,
    category: Category,
    settings: Settings,
    out_root: Path,
    state: DownloadState,
    state_path: Path,
    per_category: int,
    dry_run: bool,
) -> int:
    folder = out_root / category.folder
    folder.mkdir(parents=True, exist_ok=True)

    target = category_target(category, per_category)
    have = already_have_in_folder(folder)
    need = max(0, target - have)
    if need == 0:
        print(f"  Already have {have}/{target} — skipping.")
        return 0

    print(f"  Need {need} more (have {have}/{target})")
    candidates = collect_candidates(
        client, category, settings, state.used_ids, need
    )
    if not candidates:
        print(f"  No matching {settings.orientation} clips found for filters.")
        return 0

    downloaded = 0
    idx = next_index(folder)

    for query, video, best in candidates:
        filename = build_filename(idx, query, int(video["id"]))
        dest = folder / filename
        user = video.get("user") or {}
        clip = {
            "category": category.folder,
            "index": idx,
            "filename": filename,
            "description": description_from_query(query),
            "duration": int(video.get("duration", 0)),
            "pexels_id": int(video["id"]),
            "pexels_url": video.get("url", ""),
            "photographer": user.get("name", "Unknown"),
            "query": query,
            "width": int(best.get("width") or video.get("width") or 0),
            "height": int(best.get("height") or video.get("height") or 0),
        }

        if dry_run:
            print(
                f"  [dry-run] would download {filename} "
                f"({clip['duration']}s, {clip['width']}x{clip['height']})"
            )
        else:
            print(
                f"  Downloading {filename} "
                f"({clip['duration']}s, {clip['width']}x{clip['height']})…"
            )
            client.download_file(best["link"], dest)
            state.used_ids.add(int(video["id"]))
            state.clips = [
                c for c in state.clips if c.get("pexels_id") != clip["pexels_id"]
            ]
            state.clips.append(clip)
            state.save(state_path)

        downloaded += 1
        idx += 1

    return downloaded


def regen_manifest_from_disk(
    out_dir: Path,
    categories: list[Category],
    state: DownloadState,
    settings: Settings,
) -> None:
    clips: list[dict[str, Any]] = []
    by_id = {c["pexels_id"]: c for c in state.clips if "pexels_id" in c}

    for cat in categories:
        folder = out_dir / cat.folder
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.mp4")):
            m = re.match(r"^(\d+)-(.+)-(\d+)\.mp4$", path.name)
            if not m:
                continue
            index, slug, vid_s = int(m.group(1)), m.group(2), int(m.group(3))
            existing = by_id.get(vid_s)
            if existing:
                clips.append({**existing, "index": index, "filename": path.name})
            else:
                clips.append(
                    {
                        "category": cat.folder,
                        "index": index,
                        "filename": path.name,
                        "description": slug.replace("-", " ").capitalize(),
                        "duration": 0,
                        "pexels_id": vid_s,
                        "pexels_url": f"https://www.pexels.com/video/{vid_s}/",
                        "photographer": "Unknown",
                        "query": slug.replace("-", " "),
                        "width": 0,
                        "height": 0,
                    }
                )

    write_library_md(out_dir, categories, clips, settings)
    print(f"Wrote {out_dir / LIBRARY_MD}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download categorized stock videos from the Pexels API."
    )
    p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to config.yaml (default: ./config.yaml)",
    )
    p.add_argument(
        "--category",
        help="Only download one folder name from config.yaml",
    )
    p.add_argument(
        "--per-category",
        type=int,
        default=None,
        help="Override clips per category for this run",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and list matches without downloading",
    )
    p.add_argument(
        "--regen-manifest",
        action="store_true",
        help="Rebuild LIBRARY.md from disk + state without downloading",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()

    if not args.config.exists():
        print(
            f"Missing config: {args.config}\n"
            "Copy or create config.yaml and define your categories.",
            file=sys.stderr,
        )
        return 1

    try:
        settings, categories = load_config(args.config)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"Invalid config: {exc}", file=sys.stderr)
        return 1

    out_root = resolve_under_root(settings.output_dir, label="output_dir")
    state_path = resolve_under_root(settings.state_file, label="state_file")
    out_root.mkdir(parents=True, exist_ok=True)
    state = DownloadState.load(state_path)

    if args.regen_manifest:
        regen_manifest_from_disk(out_root, categories, state, settings)
        return 0

    api_key = os.getenv("PEXELS_API_KEY", "").strip().strip('"').strip("'")
    if not api_key or api_key.startswith("your_pexels_api_key"):
        print(
            "Set PEXELS_API_KEY in .env (copy from .env.example).\n"
            "Get a free key at https://www.pexels.com/api/",
            file=sys.stderr,
        )
        return 1
    # Never log or print the API key.

    per_category = args.per_category or settings.per_category
    selected = categories
    if args.category:
        selected = [c for c in categories if c.folder == args.category]
        if not selected:
            names = ", ".join(c.folder for c in categories)
            print(
                f"Unknown category {args.category!r}. Choose from: {names}",
                file=sys.stderr,
            )
            return 1

    client = PexelsClient(api_key, settings.request_delay_seconds)
    total = 0
    size_note = settings.size or "any"

    print(
        f"Target: {per_category} clips/category × {len(selected)} categories\n"
        f"Filters: orientation={settings.orientation}, size={size_note}, "
        f"duration={settings.min_duration}-{settings.max_duration}s\n"
        f"Output: {out_root}\n"
    )

    for cat in selected:
        print(f"\n=== {cat.folder} ===")
        if cat.description:
            print(f"  {cat.description}")
        n = download_category(
            client,
            cat,
            settings,
            out_root,
            state,
            state_path,
            per_category,
            dry_run=args.dry_run,
        )
        total += n

    if not args.dry_run:
        write_library_md(out_root, categories, state.clips, settings)
        print(f"\nWrote {out_root / LIBRARY_MD}")

    print(f"\nDone. {'Would download' if args.dry_run else 'Downloaded'} {total} clip(s).")
    if client.remaining is not None:
        print(f"API requests remaining this hour: {client.remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
