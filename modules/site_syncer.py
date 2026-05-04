"""
Site syncer module — fetches live published papers from GitHub and caches them locally.

Uses the GitHub Contents API (already authenticated via GITHUB_TOKEN) to read:
  lib/research/index.ts    — for the slug list
  lib/research/papers/*.ts — for each paper's content

Caches to OUTPUT_DIR/site_cache/ with a _meta.json sidecar tracking sync time.
"""

import os
import re
import base64
import json
from pathlib import Path
from datetime import datetime, timezone

import httpx


GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_API = "https://api.github.com"
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_file_content(path: str) -> str | None:
    """Fetch a file from GitHub and return its decoded UTF-8 content."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=_headers(), params={"ref": GITHUB_BRANCH})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8")
    except Exception as e:
        print(f"  [site_syncer] Error fetching {path}: {e}")
        return None


def _load_cache_meta(cache_dir: Path) -> dict:
    meta_path = cache_dir / "_meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_sync": None, "slugs": []}


def _save_cache_meta(cache_dir: Path, slugs: list[str]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "slugs": slugs,
    }
    (cache_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _fetch_index_slugs() -> list[str]:
    """Parse live slug list from lib/research/index.ts on GitHub."""
    content = _fetch_file_content("lib/research/index.ts")
    if not content:
        return []
    # Match: import { someVar } from "./papers/{slug}";
    return re.findall(r'from "./papers/([^"]+)"', content)


def _quick_field(ts_code: str, field: str) -> str:
    """Quick regex field extraction for display metadata (non-parsing use)."""
    for pat in [
        rf'{field}:\s*"([^"]+)"',
        rf"{field}:\s*'([^']+)'",
        rf'{field}:\s*`([^`]+)`',
    ]:
        m = re.search(pat, ts_code, re.DOTALL)
        if m:
            return m.group(1)[:400]
    return ""


# ── Public API ──────────────────────────────────────────────────────────────────

def sync_live_articles(
    output_dir: Path,
    force: bool = False,
    ttl_seconds: int = 300,
) -> dict:
    """
    Fetch all live papers from GitHub and cache them locally.

    Returns:
        success, synced_count, slugs, last_sync, errors, was_cached
    """
    cache_dir = output_dir / "site_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Respect TTL unless forced
    if not force:
        meta = _load_cache_meta(cache_dir)
        if meta.get("last_sync"):
            try:
                last = datetime.fromisoformat(meta["last_sync"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last).total_seconds()
                if age < ttl_seconds:
                    return {
                        "success": True,
                        "synced_count": len(meta.get("slugs", [])),
                        "slugs": meta.get("slugs", []),
                        "last_sync": meta["last_sync"],
                        "errors": [],
                        "was_cached": True,
                    }
            except Exception:
                pass

    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {
            "success": False,
            "synced_count": 0,
            "slugs": [],
            "last_sync": None,
            "errors": ["GITHUB_TOKEN or GITHUB_REPO not configured — cannot sync"],
            "was_cached": False,
        }

    slugs = _fetch_index_slugs()
    errors: list[str] = []
    synced: list[str] = []

    for slug in slugs:
        ts_content = _fetch_file_content(f"lib/research/papers/{slug}.ts")
        if ts_content:
            (cache_dir / f"{slug}.ts").write_text(ts_content, encoding="utf-8")
            synced.append(slug)
        else:
            errors.append(f"Failed to fetch {slug}")

    _save_cache_meta(cache_dir, synced)
    last_sync = datetime.now(timezone.utc).isoformat()

    print(f"  [site_syncer] Synced {len(synced)}/{len(slugs)} articles from GitHub ({len(errors)} errors)")

    return {
        "success": True,
        "synced_count": len(synced),
        "slugs": synced,
        "last_sync": last_sync,
        "errors": errors,
        "was_cached": False,
    }


def get_live_articles(output_dir: Path) -> list[dict]:
    """
    Return cached live articles as a list of summary dicts for the Live Site tab.
    Each dict matches the shape of get_pending_articles() + source="live_site".
    """
    cache_dir = output_dir / "site_cache"
    meta = _load_cache_meta(cache_dir)
    slugs = meta.get("slugs", [])
    last_sync = meta.get("last_sync")

    articles = []
    for slug in slugs:
        cache_path = cache_dir / f"{slug}.ts"
        if not cache_path.exists():
            continue
        ts_code = cache_path.read_text(encoding="utf-8")

        title = _quick_field(ts_code, "title")
        abstract_raw = _quick_field(ts_code, "abstract")
        abstract = (abstract_raw[:200] + "...") if len(abstract_raw) > 200 else abstract_raw
        read_time = _quick_field(ts_code, "readTime")
        published = _quick_field(ts_code, "published")
        region = _quick_field(ts_code, "region")

        articles.append({
            "slug": slug,
            "title": title or slug,
            "abstract": abstract,
            "read_time": read_time,
            "published_date": published,
            "region": region,
            "source": "live_site",
            "site_url": f"{SITE_URL}/research/{slug}",
            "last_sync": last_sync,
        })

    return articles


def get_live_article_ts(slug: str, output_dir: Path) -> str | None:
    """Return cached TypeScript content for a live article slug, or None."""
    cache_path = output_dir / "site_cache" / f"{slug}.ts"
    return cache_path.read_text(encoding="utf-8") if cache_path.exists() else None


def get_last_sync_time(output_dir: Path) -> str | None:
    """Return the ISO timestamp of the last successful sync, or None."""
    meta = _load_cache_meta(output_dir / "site_cache")
    return meta.get("last_sync")


def diff_live_vs_local(output_dir: Path) -> dict:
    """Compare live site slugs against local drafts."""
    meta = _load_cache_meta(output_dir / "site_cache")
    live_slugs = set(meta.get("slugs", []))

    articles_dir = output_dir / "articles"
    local_slugs: set[str] = set()
    if articles_dir.exists():
        local_slugs = {f.stem for f in articles_dir.glob("*.ts")}

    return {
        "live_only": sorted(live_slugs - local_slugs),
        "local_only": sorted(local_slugs - live_slugs),
        "both": sorted(live_slugs & local_slugs),
    }


def pull_live_article_for_edit(slug: str, output_dir: Path, overwrite: bool = False) -> dict:
    """
    Copy a live article's TS from the site_cache into output/articles/ for editing.
    If output/articles/{slug}.ts already exists and overwrite=False, returns
    would_overwrite=True so the frontend can confirm.
    """
    cache_path = output_dir / "site_cache" / f"{slug}.ts"
    if not cache_path.exists():
        return {
            "success": False,
            "slug": slug,
            "message": f"Article '{slug}' not in sync cache. Run a sync first.",
            "would_overwrite": False,
        }

    target_path = output_dir / "articles" / f"{slug}.ts"
    if target_path.exists() and not overwrite:
        return {
            "success": False,
            "slug": slug,
            "message": f"Article '{slug}' already exists in drafts — confirm overwrite.",
            "would_overwrite": True,
        }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(cache_path.read_text(encoding="utf-8"), encoding="utf-8")

    return {
        "success": True,
        "slug": slug,
        "message": f"'{slug}' pulled from live site to drafts for editing.",
        "would_overwrite": False,
    }
