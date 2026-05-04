"""
LinkedIn post generator — produces 3 copy-paste-ready post variants per article.
Uses Claude claude-opus-4-7 with prompt caching.
"""

import os
from pathlib import Path

import anthropic


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")

LINKEDIN_SYSTEM_PROMPT = """You are a LinkedIn content strategist for RocketPros, a collision repair software company serving Canadian shops on MPI and SGI programs. You write posts for Myles Chaput (CEO) and Ali Jakvani (Head of Product).

Your LinkedIn posts:
- Are written in first-person (as Myles or Ali, specified in the request)
- Are direct, credible, and never salesy
- Lead with a data point, insight, or scenario — never a generic opener
- Include 3–5 relevant hashtags at the end
- Always include the article link on its own line before the hashtags
- Are optimized for LinkedIn's algorithm: short paragraphs, line breaks between ideas
- Speak to collision shop owners and MPI/SGI adjusters — not generic business audiences

HASHTAG POOL (pick the most relevant 3–5):
#CollisionRepair #MPI #SGI #ADAS #BodyShop #AutoBody #CollisionIndustry
#EstimateAccuracy #CycleTime #OEM #CanadianCollision #RocketPros
#CollisionRepairCanada #MBIAccredited #SGIRepair #VehicleRepair
"""


def _generate_variants(article_title: str, article_slug: str, abstract: str, key_findings: list[str]) -> dict:
    """
    Generate 3 LinkedIn post variants for a given article.
    Returns dict with keys: hook, insight, story
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    article_url = f"{SITE_URL}/research/{article_slug}"

    findings_text = "\n".join(f"- {f}" for f in key_findings[:5])

    user_message = f"""Generate 3 LinkedIn post variants for this RocketPros research article.

ARTICLE TITLE: {article_title}
ARTICLE URL: {article_url}
ABSTRACT: {abstract}
KEY FINDINGS:
{findings_text}

Output exactly 3 variants separated by the delimiter "---VARIANT---":

VARIANT 1 — HOOK POST (120–150 words)
Purpose: Stop the scroll with a surprising or alarming data point
Structure: [One alarming stat] → [2-3 sentences context] → [Key insight] → [Article link] → [Hashtags]
Voice: Myles Chaput (CEO)

---VARIANT---

VARIANT 2 — INSIGHT POST (160–200 words)
Purpose: Thought leadership — counterintuitive truth that makes shops rethink something
Structure: [Counterintuitive insight] → [Explanation with data] → [What shops should do differently] → [Article link] → [Hashtags]
Voice: Ali Jakvani (Head of Product)

---VARIANT---

VARIANT 3 — STORY POST (150–180 words)
Purpose: Relatable scenario that makes the problem real
Structure: [Shop owner/adjuster scenario] → [The problem they face] → [What the data shows] → [The fix/takeaway] → [Article link] → [Hashtags]
Voice: Myles Chaput (CEO)

Rules:
- Each post must include the article URL on its own line
- Each post must end with hashtags (pick 3-5 from the pool)
- No generic openers like "Excited to share" or "Proud to announce"
- No emojis except ✅ → or • for lists if needed
- Short paragraphs — max 3 lines before a line break
- Output only the 3 post texts separated by ---VARIANT--- with no other commentary"""

    full_response = ""
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": LINKEDIN_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            full_response += text

    # Split on the delimiter
    parts = full_response.split("---VARIANT---")
    parts = [p.strip() for p in parts if p.strip()]

    # Clean up any "VARIANT 1 —" headers the model might have included
    cleaned = []
    for part in parts:
        lines = part.split("\n")
        # Drop first line if it looks like a variant header
        if lines and lines[0].strip().upper().startswith("VARIANT"):
            lines = lines[1:]
        cleaned.append("\n".join(lines).strip())

    return {
        "hook": cleaned[0] if len(cleaned) > 0 else "",
        "insight": cleaned[1] if len(cleaned) > 1 else "",
        "story": cleaned[2] if len(cleaned) > 2 else "",
    }


def generate_linkedin_posts(article: dict) -> dict:
    """
    Generate 3 LinkedIn variants for an article dict.

    Args:
        article: dict with keys: title, slug, ts_code (plus topic info)

    Returns:
        dict with keys: hook, insight, story (each a ready-to-paste string)
    """
    title = article.get("title", "")
    slug = article.get("slug", "")

    # Extract abstract and key findings from the raw TypeScript via regex
    ts_code = article.get("ts_code", "")
    abstract = _extract_field(ts_code, "abstract")
    key_findings = _extract_array_field(ts_code, "keyFindings")

    print(f"  [linkedin_generator] Generating 3 variants for: '{title}'")
    variants = _generate_variants(title, slug, abstract, key_findings)
    print(f"  [linkedin_generator] Done.")
    return variants


def _extract_field(ts_code: str, field: str) -> str:
    """Simple regex extraction of a string field from TypeScript."""
    import re
    # Try double-quoted string
    pattern = rf'{field}:\s*"([^"]+)"'
    match = re.search(pattern, ts_code, re.DOTALL)
    if match:
        return match.group(1).replace("\\n", " ").strip()
    # Try single-quoted string
    pattern2 = rf"{field}:\s*'([^']+)'"
    match2 = re.search(pattern2, ts_code, re.DOTALL)
    if match2:
        return match2.group(1).replace("\\n", " ").strip()
    # Try template literal
    pattern3 = rf'{field}:\s*`([^`]+)`'
    match3 = re.search(pattern3, ts_code, re.DOTALL)
    if match3:
        return match3.group(1).replace("\n", " ").strip()
    return ""


def _extract_array_field(ts_code: str, field: str) -> list[str]:
    """Extract string array items from TypeScript source."""
    import re
    # Find the array block — handle nested brackets
    pattern = rf'{field}:\s*\['
    match = re.search(pattern, ts_code)
    if not match:
        return []
    start = match.end()
    depth = 1
    i = start
    while i < len(ts_code) and depth > 0:
        if ts_code[i] == '[':
            depth += 1
        elif ts_code[i] == ']':
            depth -= 1
        i += 1
    block = ts_code[start:i - 1]
    # Extract quoted strings of reasonable length
    items = re.findall(r'"([^"]{15,})"', block)
    if not items:
        items = re.findall(r"'([^']{15,})'", block)
    return items


def save_linkedin_posts(article: dict, variants: dict, output_dir: Path) -> Path:
    """Save LinkedIn posts to output/linkedin/<slug>.txt"""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = article["slug"]
    file_path = output_dir / f"{slug}.txt"

    content = f"LinkedIn Posts — {article['title']}\n"
    content += "=" * 60 + "\n\n"

    content += "VARIANT 1 — HOOK POST\n"
    content += "-" * 40 + "\n"
    content += variants.get("hook", "") + "\n\n"

    content += "VARIANT 2 — INSIGHT POST\n"
    content += "-" * 40 + "\n"
    content += variants.get("insight", "") + "\n\n"

    content += "VARIANT 3 — STORY POST\n"
    content += "-" * 40 + "\n"
    content += variants.get("story", "") + "\n"

    file_path.write_text(content, encoding="utf-8")
    print(f"  [linkedin_generator] Saved: {file_path}")
    return file_path
