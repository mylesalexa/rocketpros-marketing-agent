"""
Publisher module — pushes approved articles to the rprosite-main GitHub repo
via the GitHub API. Triggers a Vercel auto-deploy on every push.

What it does in one atomic GitHub commit:
  1. Creates /lib/research/papers/<slug>.ts
  2. Creates /public/images/<slug>.png
  3. Updates /lib/research/index.ts (adds import + array entry)

Required env vars:
  GITHUB_TOKEN  — Personal Access Token with repo write access
  GITHUB_REPO   — e.g., "mylesalexa/rprosite-main"
  GITHUB_BRANCH — default "main"
"""

import os
import base64
import re
from pathlib import Path
from datetime import datetime

import httpx


GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")   # e.g. "mylesalexa/rprosite-main"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

GITHUB_API = "https://api.github.com"

OUTPUT_DIR = Path(__file__).parent.parent / "output"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_file(path: str) -> tuple[str, str] | tuple[None, None]:
    """
    Fetch a file from GitHub. Returns (content_str, sha) or (None, None) if not found.
    """
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    params = {"ref": GITHUB_BRANCH}
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, headers=_headers(), params=params)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def _put_file(path: str, content_bytes: bytes, message: str, sha: str | None = None) -> bool:
    """
    Create or update a file on GitHub. Returns True on success.
    """
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    with httpx.Client(timeout=30.0) as client:
        resp = client.put(url, headers=_headers(), json=payload)

    if resp.status_code not in (200, 201):
        print(f"  [publisher] GitHub API error {resp.status_code}: {resp.text[:300]}")
        return False
    return True


def _slug_to_camel(slug: str) -> str:
    parts = slug.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _update_index_ts(current_content: str, slug: str, camel_name: str) -> str:
    """
    Insert a new import and array entry into index.ts.
    Inserts import after the last existing import line.
    Inserts array entry at the top of the papers[] array (Canada-first).
    """
    new_import = f'import {{ {camel_name} }} from "./papers/{slug}";'

    # Don't add duplicate
    if new_import in current_content:
        return current_content

    # Insert import after the last import line
    lines = current_content.split("\n")
    last_import_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("import "):
            last_import_idx = i

    lines.insert(last_import_idx + 1, new_import)
    content_with_import = "\n".join(lines)

    # Insert into papers[] array — after the "Canada-first ordering" comment if present, else first
    array_entry = f"  {camel_name},"
    if "// Canada-first ordering" in content_with_import:
        content_with_import = content_with_import.replace(
            "// Canada-first ordering — RocketPros' primary audience\n",
            f"// Canada-first ordering — RocketPros' primary audience\n  {camel_name},\n"
        )
    else:
        # Insert as first item in the array
        content_with_import = re.sub(
            r"(export const papers: Paper\[\] = \[)\n",
            f"\\1\n{array_entry}\n",
            content_with_import
        )

    return content_with_import


def _validate_before_publish(ts_content: str, slug: str) -> list[str]:
    """
    Final safety check before pushing to GitHub.
    Blocks truncated or structurally incomplete files from ever reaching Vercel.
    """
    errors = []
    stripped = ts_content.strip()

    if not stripped.endswith("};"):
        errors.append(
            f"File does not end with '}};' — response was truncated. "
            f"Last 80 chars: ...{stripped[-80:]!r}"
        )

    required = ["citations:", "faq:", "shopImplications:", "carrierImplications:", "sections:"]
    for arr in required:
        if arr not in ts_content:
            errors.append(f"Missing required field '{arr}' — file is incomplete")

    if "export const" not in ts_content:
        errors.append("Missing 'export const' declaration — invalid TypeScript")

    if 'import type { Paper }' not in ts_content and "import type { Paper }" not in ts_content:
        errors.append("Missing 'import type { Paper }' — invalid TypeScript")

    return errors


def publish_article(slug: str) -> dict:
    """
    Publish an approved article to the rprosite-main GitHub repo.

    Args:
        slug: The article slug (e.g., "adas-calibration-mpi-sgi")

    Returns:
        dict with keys: success (bool), message (str), url (str)
    """
    if not GITHUB_TOKEN:
        return {"success": False, "message": "GITHUB_TOKEN not set", "url": ""}
    if not GITHUB_REPO:
        return {"success": False, "message": "GITHUB_REPO not set", "url": ""}

    # Locate local files
    ts_path = OUTPUT_DIR / "articles" / f"{slug}.ts"
    img_path = OUTPUT_DIR / "images" / f"{slug}.png"

    if not ts_path.exists():
        return {"success": False, "message": f"Article file not found: {ts_path}", "url": ""}

    ts_content = ts_path.read_text(encoding="utf-8")

    # ── Pre-publish structural validation ──────────────────────────────────────
    pre_errors = _validate_before_publish(ts_content, slug)
    if pre_errors:
        error_detail = " | ".join(pre_errors)
        print(f"  [publisher] ✗ BLOCKED — article '{slug}' failed pre-publish validation:")
        for err in pre_errors:
            print(f"    - {err}")
        return {
            "success": False,
            "message": f"Article blocked — structural issues detected: {error_detail}",
            "url": "",
        }
    camel_name = _slug_to_camel(slug)
    today = datetime.now().strftime("%Y-%m-%d")
    commit_message = f"feat: add research article '{slug}' ({today})"

    print(f"  [publisher] Publishing '{slug}' to {GITHUB_REPO}...")

    # ── 1. Push the TypeScript paper file ──────────────────────────────────────
    paper_github_path = f"lib/research/papers/{slug}.ts"
    _, existing_sha = _get_file(paper_github_path)
    ok = _put_file(
        paper_github_path,
        ts_content.encode("utf-8"),
        commit_message,
        sha=existing_sha,
    )
    if not ok:
        return {"success": False, "message": f"Failed to push {paper_github_path}", "url": ""}
    print(f"  [publisher] ✓ Pushed {paper_github_path}")

    # ── 2. Push the hero image (if it exists) ──────────────────────────────────
    if img_path.exists():
        image_github_path = f"public/images/{slug}.png"
        _, img_sha = _get_file(image_github_path)
        img_ok = _put_file(
            image_github_path,
            img_path.read_bytes(),
            commit_message,
            sha=img_sha,
        )
        if img_ok:
            print(f"  [publisher] ✓ Pushed {image_github_path}")
        else:
            print(f"  [publisher] ⚠ Image push failed (continuing without image)")
    else:
        print(f"  [publisher] ⚠ No image found at {img_path} (skipping)")

    # ── 3. Update lib/research/index.ts ────────────────────────────────────────
    index_path = "lib/research/index.ts"
    current_index, index_sha = _get_file(index_path)
    if current_index is None:
        return {"success": False, "message": "Could not fetch lib/research/index.ts from GitHub", "url": ""}

    updated_index = _update_index_ts(current_index, slug, camel_name)
    if updated_index == current_index:
        print(f"  [publisher] index.ts already contains {slug}, skipping")
    else:
        idx_ok = _put_file(
            index_path,
            updated_index.encode("utf-8"),
            commit_message,
            sha=index_sha,
        )
        if not idx_ok:
            return {"success": False, "message": "Failed to update lib/research/index.ts", "url": ""}
        print(f"  [publisher] ✓ Updated lib/research/index.ts")

    site_url = os.getenv("SITE_URL", "https://rocketpros.app")
    article_url = f"{site_url}/research/{slug}"
    print(f"  [publisher] ✓ Done — deploying to {article_url}")

    return {
        "success": True,
        "message": f"Published successfully. Vercel will deploy in ~60 seconds.",
        "url": article_url,
    }


def get_pending_articles() -> list[dict]:
    """
    Return all articles in output/articles/ as a list of dicts.
    """
    articles_dir = OUTPUT_DIR / "articles"
    if not articles_dir.exists():
        return []

    articles = []
    for ts_file in sorted(articles_dir.glob("*.ts"), key=lambda f: f.stat().st_mtime, reverse=True):
        slug = ts_file.stem
        ts_content = ts_file.read_text(encoding="utf-8")

        # Extract title
        title_match = re.search(r'title:\s*"([^"]+)"', ts_content)
        title = title_match.group(1) if title_match else slug

        # Extract abstract
        abstract_match = re.search(r'abstract:\s*"([^"]{20,})"', ts_content)
        abstract = abstract_match.group(1)[:200] + "..." if abstract_match else ""

        # Extract readTime
        read_time_match = re.search(r'readTime:\s*"([^"]+)"', ts_content)
        read_time = read_time_match.group(1) if read_time_match else ""

        # Check if image exists
        has_image = (OUTPUT_DIR / "images" / f"{slug}.png").exists()

        # Check if LinkedIn posts exist
        has_linkedin = (OUTPUT_DIR / "linkedin" / f"{slug}.txt").exists()

        # Check if already published (exists in GitHub)
        articles.append({
            "slug": slug,
            "title": title,
            "abstract": abstract,
            "read_time": read_time,
            "has_image": has_image,
            "has_linkedin": has_linkedin,
            "modified": datetime.fromtimestamp(ts_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    return articles


def get_linkedin_posts(slug: str) -> dict:
    """Load LinkedIn posts for a given slug."""
    linkedin_file = OUTPUT_DIR / "linkedin" / f"{slug}.txt"
    if not linkedin_file.exists():
        return {}

    content = linkedin_file.read_text(encoding="utf-8")
    posts = {}

    for variant, label in [("hook", "VARIANT 1"), ("insight", "VARIANT 2"), ("story", "VARIANT 3")]:
        pattern = rf"{label}[^\n]*\n-{{40}}\n(.*?)(?=VARIANT \d|$)"
        match = re.search(pattern, content, re.DOTALL)
        if match:
            posts[variant] = match.group(1).strip()

    return posts
