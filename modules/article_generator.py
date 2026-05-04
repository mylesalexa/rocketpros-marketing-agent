"""
Article generator — uses Claude claude-opus-4-7 with prompt caching to produce
Paper TypeScript objects matching /lib/research/types.ts exactly.

Prompt caching strategy:
  - System prompt + Paper type definition: cached (stable across runs)
  - Per-topic user message: not cached (changes each call)

Estimated cost per article: ~$0.08–0.15 with caching enabled.

CRITICAL SCHEMA FACTS (match types.ts exactly):
  - Author: { name, title }          — NOT "role"
  - FAQ:    { q, a }                 — NOT "question"/"answer"
  - Citation: { label, url? }        — NOT "text", NO "id"
  - readTime: "13 min read"          — include "read"
  - import: import type { Paper }    — NOT import { Paper }
"""

import os
import re
from datetime import datetime
from pathlib import Path

import anthropic


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")

SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "templates" / "system_prompt.txt"

# Token budget: covers thinking + full article output.
# 8192 was too low — adaptive thinking consumes 2-4K tokens, leaving insufficient
# room for a complete 2500-word TypeScript article. 16000 gives ample headroom.
MAX_TOKENS = 16000

# Retry on truncation: attempt with progressively higher token budgets
MAX_RETRIES = 3
RETRY_TOKEN_MULTIPLIERS = [1, 1.5, 2.0]  # × MAX_TOKENS on each attempt


def _load_system_prompt() -> str:
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _slug_to_camel(slug: str) -> str:
    """Convert 'adas-calibration-mpi' → 'adasCalibrationMpi'"""
    parts = slug.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _generate_slug(title: str) -> str:
    """Convert a title to a URL-safe slug, max 8 words."""
    slug = title.lower()
    # Remove subtitle if present (after colon)
    slug = slug.split(":")[0].strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    parts = slug.split("-")
    if len(parts) > 8:
        slug = "-".join(parts[:8])
    return slug


def _extract_typescript_from_response(text: str) -> str:
    """
    Extract the TypeScript code from Claude's response.
    Handles cases where the model wraps in markdown despite instructions.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)
    return text.strip()


def _extract_slug_from_typescript(ts_code: str) -> str | None:
    match = re.search(r'slug:\s*["\']([^"\']+)["\']', ts_code)
    return match.group(1) if match else None


def _extract_title_from_typescript(ts_code: str) -> str | None:
    match = re.search(r'title:\s*["\']([^"\']+)["\']', ts_code)
    return match.group(1) if match else None


def _check_truncation(ts_code: str) -> list[str]:
    """
    Structural completeness check — detects truncated output before it reaches
    the publisher. A complete Paper TypeScript file must end with '};' and
    contain all required top-level arrays.
    """
    errors = []
    stripped = ts_code.strip()

    if not stripped.endswith("};"):
        errors.append(
            f"File does not end with '}};' — response was cut off mid-generation "
            f"(last 60 chars: ...{stripped[-60:]!r})"
        )

    required_arrays = ["citations:", "faq:", "shopImplications:", "carrierImplications:", "sections:"]
    for arr in required_arrays:
        if arr not in ts_code:
            errors.append(f"Missing required array '{arr}' — likely truncated before it was written")

    return errors


def generate_article(topic: dict) -> dict:
    """
    Generate a full Paper TypeScript object for the given topic.
    Retries up to MAX_RETRIES times on truncation, increasing max_tokens each time.

    Args:
        topic: dict with keys: title, angle, audience, source_urls

    Returns:
        dict with keys:
            - ts_code: str (full TypeScript file content, ready to paste)
            - slug: str
            - title: str
            - validation_errors: list[str]
            - truncation_errors: list[str]
            - token_usage: dict
            - topic: dict
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = _load_system_prompt()
    today = datetime.now().strftime("%Y-%m-%d")
    topic_title = topic.get("title", "Unknown Topic")
    topic_angle = topic.get("angle", "")
    topic_audience = topic.get("audience", "Both shops and insurers")
    source_urls = topic.get("source_urls", [])

    suggested_slug = _generate_slug(topic_title)
    camel_slug = _slug_to_camel(suggested_slug)

    direction = topic.get("direction", "").strip()

    source_context = ""
    if source_urls:
        source_context = "\n\nResearch sources found during topic discovery (reference for citations where relevant):\n"
        source_context += "\n".join(f"- {url}" for url in source_urls[:5])

    direction_context = ""
    if direction:
        direction_context = f"\n\nSPECIFIC DIRECTION FROM EDITOR: {direction}\nThis is the specific angle, focus, or subject the article must cover. Follow this direction precisely — use it to determine the article's thesis, which sections to emphasize, and which OEM/program/carrier details to research and include."

    user_message = f"""Generate a complete RocketPros research article as a TypeScript Paper object.

TOPIC: {topic_title}
ANGLE: {topic_angle}
PRIMARY AUDIENCE: {topic_audience}
PUBLISHED DATE: {today}
SUGGESTED SLUG: {suggested_slug}
CAMELCASE EXPORT NAME: {camel_slug}
SITE URL: {SITE_URL}{direction_context}{source_context}

CRITICAL SCHEMA REQUIREMENTS — match types.ts exactly:
- Author fields: {{ name: "...", title: "Co-founder, RocketPros" }}  ← "title" NOT "role"
- FAQ fields: {{ q: "...", a: "..." }}  ← "q" and "a" NOT "question"/"answer"
- Citation fields: {{ label: "Full descriptive source string.", url: "https://..." }}  ← "label" NOT "text", NO "id" field
- readTime format: "13 min read"  ← include "read"
- import line: import type {{ Paper }} from "../types";  ← use "import type"

TITLE: Write a specific, practitioner-facing title like existing RocketPros papers:
  Good: "Pre- and Post-Repair Scanning on MPI Claims: What the LVT Requires and Why It Matters"
  Bad: "{topic_title}"  ← too generic, rewrite it

AUTHORS: Always use exactly these two:
  {{ name: "Myles Chaput", title: "Co-founder, RocketPros" }}
  {{ name: "Ali Jakvani", title: "Co-founder, RocketPros" }}

REQUIRED SECTIONS (numbered H2s):
  1. What [topic] is — define the concept precisely for MPI/SGI context
  2–4. Mechanics/rules — how the program/procedure actually works, with tables
  5. Where shops typically lose ground — specific, avoidable failure modes
  6. How RocketPros aligns to [topic] — program-aligned, non-promotional framing
  7. The carrier perspective — MPI/SGI program-management view

CITATION STANDARD (match existing papers):
  {{ label: "Manitoba Public Insurance — Body Shop & Glass Information portal (program documents, bulletins, accreditation framework, Light Vehicle Tariff distribution).", url: "https://www.mpi.mb.ca/" }}
  {{ label: "Statistics Canada, Consumer Price Index — vehicle parts, maintenance and repairs (Table 18-10-0004-01).", url: "https://www150.statcan.gc.ca" }}
  Include 7–12 citations. Use: MPI portal, SGI portal, provincial legislation, IBC, Statistics Canada, I-CAR RTS, OEM1Stop.com, CCC Crash Course, Mitchell Industry Trends, CCIF, IIHS-HLDI.

IMPORTANT: Write the COMPLETE article without stopping. All arrays must be fully closed.
The file must end with exactly:   }};
Do not stop generating until the final '}};' is written.

Output ONLY the TypeScript file. No prose. No markdown fences. Start with:
import type {{ Paper }} from "../types";"""

    print(f"  [article_generator] Generating article: '{topic_title}'")

    ts_code = ""
    truncation_errors: list[str] = []
    token_usage: dict = {}

    for attempt in range(1, MAX_RETRIES + 1):
        tokens_this_attempt = int(MAX_TOKENS * RETRY_TOKEN_MULTIPLIERS[attempt - 1])

        if attempt > 1:
            print(f"  [article_generator] Retry {attempt}/{MAX_RETRIES} with max_tokens={tokens_this_attempt}...")

        full_response = ""
        stop_reason = "unknown"

        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=tokens_this_attempt,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {"role": "user", "content": user_message}
                ],
            ) as stream:
                for text in stream.text_stream:
                    full_response += text

                final = stream.get_final_message()
                usage = final.usage
                stop_reason = final.stop_reason

                token_usage = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
                    "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0),
                }

        except Exception as e:
            print(f"  [article_generator] ✗ API error on attempt {attempt}: {e}")
            if attempt == MAX_RETRIES:
                raise
            continue

        print(
            f"  [article_generator] Attempt {attempt}: stop_reason={stop_reason!r}, "
            f"tokens: {token_usage.get('input_tokens', 0)} in / "
            f"{token_usage.get('output_tokens', 0)} out "
            f"({token_usage.get('cache_read_tokens', 0)} cache read)"
        )

        # ── Truncation check ─────────────────────────────────────────────────
        if stop_reason == "max_tokens":
            print(f"  [article_generator] ⚠ Hit max_tokens ({tokens_this_attempt}) — response truncated.")
            if attempt < MAX_RETRIES:
                continue
            else:
                print(f"  [article_generator] ✗ All {MAX_RETRIES} attempts hit max_tokens. Keeping partial output.")

        ts_code = _extract_typescript_from_response(full_response)
        truncation_errors = _check_truncation(ts_code)

        if truncation_errors:
            print(f"  [article_generator] ⚠ Attempt {attempt}: structural truncation detected:")
            for err in truncation_errors:
                print(f"    - {err}")
            if attempt < MAX_RETRIES:
                continue
            else:
                print(f"  [article_generator] ✗ Could not produce complete article after {MAX_RETRIES} attempts.")
        else:
            print(f"  [article_generator] ✓ Article is structurally complete.")
            break

    slug = _extract_slug_from_typescript(ts_code) or suggested_slug
    title = _extract_title_from_typescript(ts_code) or topic_title
    validation_errors = _validate_typescript_output(ts_code)

    if validation_errors:
        print(f"  [article_generator] Schema validation warnings:")
        for err in validation_errors:
            print(f"    - {err}")

    return {
        "ts_code": ts_code,
        "slug": slug,
        "title": title,
        "validation_errors": validation_errors,
        "truncation_errors": truncation_errors,
        "token_usage": token_usage,
        "topic": topic,
    }


def _validate_typescript_output(ts_code: str) -> list[str]:
    """
    Regex-level validation of the generated TypeScript.
    Checks for correct field names matching types.ts.
    Returns list of warning strings.
    """
    errors = []

    if "import type { Paper }" not in ts_code and 'import type { Paper }' not in ts_code:
        if "import { Paper }" in ts_code:
            errors.append("Use 'import type { Paper }' not 'import { Paper }'")
        else:
            errors.append("Missing: import type { Paper } from '../types'")

    if "export const" not in ts_code:
        errors.append("Missing: export const <name>: Paper = {...}")

    # Schema field name checks
    if re.search(r'\brole:\s*["\']', ts_code):
        errors.append("Author has 'role' field — should be 'title'")

    if re.search(r'\bquestion:\s*["\']', ts_code):
        errors.append("FAQ has 'question' field — should be 'q'")

    if re.search(r'\banswer:\s*["\']', ts_code):
        errors.append("FAQ has 'answer' field — should be 'a'")

    if re.search(r'citations.*?\bid:\s*\d', ts_code, re.DOTALL):
        errors.append("Citation has 'id' field — citations use { label, url? } only")

    if re.search(r'citations.*?\btext:\s*["\']', ts_code, re.DOTALL):
        errors.append("Citation has 'text' field — should be 'label'")

    # Content checks
    table_count = len(re.findall(r'type:\s*["\']table["\']', ts_code))
    if table_count < 2:
        errors.append(f"Only {table_count} table(s) — need at least 2")

    faq_count = len(re.findall(r'\bq:\s*["\']', ts_code))
    if faq_count < 5:
        errors.append(f"Only {faq_count} FAQ q: fields — need at least 5")

    label_count = len(re.findall(r'\blabel:\s*["\']', ts_code))
    if label_count < 7:
        errors.append(f"Only {label_count} citation label(s) — need at least 7")

    if "Myles Chaput" not in ts_code:
        errors.append("Missing author: Myles Chaput")

    if "RocketPros aligns" not in ts_code and "RocketPros align" not in ts_code:
        errors.append("Missing 'How RocketPros aligns' section")

    if '"Canada"' not in ts_code and "'Canada'" not in ts_code:
        errors.append("region must be 'Canada'")

    return errors


def save_article(article: dict, output_dir: Path) -> Path:
    """Save generated TypeScript to output/articles/<slug>.ts"""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = article["slug"]
    file_path = output_dir / f"{slug}.ts"
    file_path.write_text(article["ts_code"], encoding="utf-8")
    print(f"  [article_generator] Saved: {file_path}")
    return file_path
