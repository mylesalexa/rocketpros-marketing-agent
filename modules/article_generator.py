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

# Approved domains — mirrors APPROVED_DOMAINS in researcher.py.
# Kept here so validation is self-contained without circular imports.
_APPROVED_CITATION_DOMAINS = {
    "i-car.com", "rts.i-car.com",
    "scrs.com",
    "asashop.org",
    "collisionweek.com",
    "repairerdrivennews.com",
    "cccis.com",
    "mitchell.com", "mitchellrepair.com",
    "audatex.com", "solera.com",
    "nhtsa.gov",
    "tc.gc.ca",
    "mpi.mb.ca",
    "sgi.sk.ca",
    "icbc.com",
    "ibc.ca",
    "ccif.ca",
    "insuranceinstitute.ca",
    "statcan.gc.ca",
    "iihs.org",
    "assuredperformance.net",
    "bodyshopbusiness.com",
    "autobodynews.com",
    "insurancejournal.com",
    "oem1stop.com",
}

# Generic AI filler phrases that indicate low-quality output.
_SLOP_PHRASES = [
    "in today's fast-paced",
    "in the ever-evolving",
    "it's no secret that",
    "the landscape is changing",
    "needless to say",
    "at the end of the day",
    "as we move forward",
    "the collision repair industry is evolving",
    "exciting times",
    "we're excited to",
    "proud to announce",
    "it goes without saying",
    "rest assured",
    "in today's competitive",
    "in a rapidly changing",
    "the world of collision repair",
    "in recent years, the industry has seen",
]

_CURRENT_YEAR = datetime.now().year

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


_SLUG_STRIP_PREFIXES = re.compile(
    r"^(create\s+(an?\s+)?article\s+(about|on|covering|regarding)\s*[:\-]?\s*"
    r"|write\s+(an?\s+)?article\s+(about|on|covering)\s*[:\-]?\s*"
    r"|article\s+(about|on|covering)\s*[:\-]?\s*"
    r"|write\s+about\s*[:\-]?\s*"
    r"|how\s+to\s*[:\-]?\s*"
    r"|guide\s+to\s*[:\-]?\s*"
    r"|topic\s*[:\-]?\s*)",
    re.IGNORECASE,
)


def _generate_slug(title: str) -> str:
    """Convert a title to a URL-safe slug, max 8 words.
    Strips common instructional prefixes that appear in editor topic fields.
    """
    slug = _SLUG_STRIP_PREFIXES.sub("", title).strip()
    # Remove subtitle after colon
    slug = slug.split(":")[0].strip()
    slug = slug.lower()
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
    text = text.strip()
    # Safety net: replace em dashes that slipped past the prompt prohibition
    text = text.replace("—", " - ")
    return text


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
        source_context = "\n\nResearch sources found during topic discovery (use these as citation anchors where relevant):\n"
        for src in source_urls[:5]:
            # source_urls may be plain strings (legacy) or dicts with trust metadata
            if isinstance(src, dict):
                url = src.get("url", "")
                trusted = src.get("trusted", False)
                recency = src.get("recency", "unknown")
                trust_label = " [APPROVED SOURCE]" if trusted else ""
                recency_label = " [RECENT]" if recency == "recent" else (" [DATED - use sparingly]" if recency == "dated" else "")
                source_context += f"- {url}{trust_label}{recency_label}\n"
            else:
                source_context += f"- {src}\n"

        # Warn the model if trusted sources are scarce
        trusted_count = topic.get("trusted_source_count", None)
        if trusted_count is not None and trusted_count == 0:
            source_context += "\nNOTE: No approved-domain sources were found in search results for this topic. "
            source_context += "If you cannot cite at least 3 credible sources from the approved list, "
            source_context += "state 'Limited recent source material was available for this topic.' in the article."

    direction_context = ""
    if direction:
        direction_context = f"\n\nSPECIFIC DIRECTION FROM EDITOR: {direction}\nThis is the specific angle, focus, or subject the article must cover. Follow this direction precisely — use it to determine the article's thesis, which sections to emphasize, and which OEM/program/carrier details to research and include."

    # Region-aware citation guidance
    topic_lower = (topic_title + " " + topic_angle + " " + direction).lower()
    if any(kw in topic_lower for kw in ["mpi", "sgi", "manitoba", "saskatchewan", "lvaa", "light vehicle accreditation"]):
        citation_guidance = """Include 7–12 citations from: MPI portal (mpi.mb.ca), SGI portal (sgi.sk.ca), provincial legislation (Manitoba Public Insurance Corporation Act / Saskatchewan Government Insurance Act), Insurance Bureau of Canada (ibc.ca), Statistics Canada Table 18-10-0004-01 (statcan.gc.ca), I-CAR RTS (rts.i-car.com), OEM1Stop.com, CCC Crash Course (cccis.com), Mitchell Industry Trends, CCIF, IIHS-HLDI.
Set region: "Canada" in the Paper object."""
    elif any(kw in topic_lower for kw in ["state farm", "geico", "progressive", "allstate", "farmers", "us drp", "united states", "american", "usa"]):
        citation_guidance = """Include 7–12 citations from: Society of Collision Repair Specialists / SCRS (scrs.com), Automotive Service Association / ASA (asashop.org), CCC Intelligent Solutions Crash Course (cccis.com), Mitchell Industry Trends (mitchellrepair.com), IIHS-HLDI (iihs.org), NHTSA (nhtsa.gov), Repairer Driven News (repairerdrivennews.com), I-CAR RTS (rts.i-car.com), Assured Performance Network (assuredperformance.net), state DOI where relevant.
Set region: "United States" in the Paper object."""
    else:
        citation_guidance = """Include 7–12 citations drawing from both Canadian and US sources: Insurance Bureau of Canada (ibc.ca), Statistics Canada (statcan.gc.ca), CCC Crash Course (cccis.com), SCRS (scrs.com), ASA (asashop.org), Mitchell Industry Trends, IIHS-HLDI (iihs.org), I-CAR RTS (rts.i-car.com), MPI portal (mpi.mb.ca), SGI portal (sgi.sk.ca), CCIF, Repairer Driven News (repairerdrivennews.com).
Set region: "North America" in the Paper object."""

    user_message = f"""Generate a complete RocketPros research article as a TypeScript Paper object.

TOPIC: {topic_title}
ANGLE: {topic_angle}
PRIMARY AUDIENCE: {topic_audience}
PUBLISHED DATE: {today}
SLUG GUIDANCE: derive the slug from the practitioner-facing title you write — NOT from this guidance string. Use only lowercase letters, numbers, and hyphens. Max 8 words. Example: "pre-post-repair-scanning-mpi-lvaa" for an article titled "Pre- and Post-Repair Scanning on MPI Claims: What the LVAA Requires and Why It Matters".
CAMELCASE EXPORT NAME: derive from your slug (e.g. slug "pre-post-repair-scanning-mpi-lvt" → export name "prePostRepairScanningMpiLvt")
SITE URL: {SITE_URL}{direction_context}{source_context}

CRITICAL SCHEMA REQUIREMENTS — match types.ts exactly:
- Author fields: {{ name: "...", title: "Co-founder, RocketPros" }}  ← "title" NOT "role"
- FAQ fields: {{ q: "...", a: "..." }}  ← "q" and "a" NOT "question"/"answer"
- Citation fields: {{ label: "Full descriptive source string.", url: "https://..." }}  ← "label" NOT "text", NO "id" field
- readTime format: "13 min read"  ← include "read"
- import line: import type {{ Paper }} from "../types";  ← use "import type"

TITLE: Write a specific, practitioner-facing title like existing RocketPros papers:
  Good: "Pre- and Post-Repair Scanning on MPI Claims: What the LVAA Requires and Why It Matters"
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

CITATION STANDARD (match existing papers exactly — full descriptive labels, no id field):
  {{ label: "Manitoba Public Insurance — Body Shop & Glass Information portal.", url: "https://www.mpi.mb.ca/" }}
  {{ label: "CCC Intelligent Solutions, Crash Course Report, 2024 Edition — US repairable severity benchmarks.", url: "https://cccis.com" }}
  {{ label: "Society of Collision Repair Specialists (SCRS), Repair Segment Profile.", url: "https://www.scrs.com" }}
  {citation_guidance}

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
    validation_errors, quality_flags = _validate_typescript_output(ts_code)

    if validation_errors:
        print(f"  [article_generator] Schema validation warnings:")
        for err in validation_errors:
            print(f"    - {err}")

    if quality_flags:
        print(f"  [article_generator] Quality flags:")
        for flag in quality_flags:
            print(f"    ⚠  {flag}")

    return {
        "ts_code": ts_code,
        "slug": slug,
        "title": title,
        "validation_errors": validation_errors,
        "quality_flags": quality_flags,
        "truncation_errors": truncation_errors,
        "token_usage": token_usage,
        "topic": topic,
    }


def _validate_typescript_output(ts_code: str) -> tuple[list[str], list[str]]:
    """
    Regex-level validation of the generated TypeScript.
    Checks schema correctness, content minimums, anti-slop, and citation quality.

    Returns:
        (errors, quality_flags)
        errors:        hard schema issues (wrong field names, missing required sections)
        quality_flags: soft quality warnings surfaced in email digest
    """
    errors = []
    quality_flags = []

    # ── Schema checks ────────────────────────────────────────────────────────────
    if "import type { Paper }" not in ts_code and 'import type { Paper }' not in ts_code:
        if "import { Paper }" in ts_code:
            errors.append("Use 'import type { Paper }' not 'import { Paper }'")
        else:
            errors.append("Missing: import type { Paper } from '../types'")

    if "export const" not in ts_code:
        errors.append("Missing: export const <name>: Paper = {...}")

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

    # ── Content minimum checks ───────────────────────────────────────────────────
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

    # Region validation — all three valid values are acceptable
    valid_regions = ['"Canada"', "'Canada'", '"United States"', "'United States'", '"North America"', "'North America'"]
    if not any(r in ts_code for r in valid_regions):
        errors.append("region field missing or not one of: Canada, United States, North America")

    # ── Citation quality checks (quality flags, not hard errors) ─────────────────
    citation_urls = re.findall(r'url:\s*["\']([^"\']+)["\']', ts_code)
    citation_labels = re.findall(r'label:\s*["\']([^"\']+)["\']', ts_code)

    # Check how many citations point to approved domains
    approved_count = sum(1 for url in citation_urls if _is_trusted_citation_url(url))
    if citation_urls and approved_count == 0:
        quality_flags.append("QUALITY_WARN: no_approved_domain_citations — all citation URLs are from unknown sources")
    elif citation_urls and approved_count < 3:
        quality_flags.append(f"QUALITY_WARN: low_approved_citations — only {approved_count}/{len(citation_urls)} citations from approved domains")

    # Check for currentness signal in citation labels
    current_year_str = str(_CURRENT_YEAR)
    prior_year_str = str(_CURRENT_YEAR - 1)
    has_current_year = any(
        current_year_str in label or prior_year_str in label
        for label in citation_labels
    )
    if citation_labels and not has_current_year:
        quality_flags.append(f"QUALITY_WARN: no_recent_sources — no citation labels reference {prior_year_str} or {current_year_str}")

    # ── Anti-slop detection ──────────────────────────────────────────────────────
    ts_lower = ts_code.lower()
    slop_found = [phrase for phrase in _SLOP_PHRASES if phrase in ts_lower]
    if len(slop_found) >= 2:
        quality_flags.append(f"QUALITY_WARN: slop_phrases_detected — found {len(slop_found)} generic filler phrases: {slop_found[:3]}")
    elif len(slop_found) == 1:
        quality_flags.append(f"QUALITY_WARN: slop_phrase_detected — found generic filler: \"{slop_found[0]}\"")

    # ── Fabrication guard — detect "according to FirstName LastName" patterns ─────
    # These are flagged when the named person doesn't appear in any known source
    quote_names = re.findall(r'according to ([A-Z][a-z]+ [A-Z][a-z]+)', ts_code)
    known_authors = {"Myles Chaput", "Ali Jakvani"}
    suspicious_quotes = [name for name in quote_names if name not in known_authors]
    if suspicious_quotes:
        quality_flags.append(
            f"QUALITY_WARN: possible_hallucinated_quote — verify these named sources exist: {suspicious_quotes}"
        )

    return errors, quality_flags


def _is_trusted_citation_url(url: str) -> bool:
    """Check if a citation URL belongs to an approved high-authority domain."""
    try:
        host = url.lower().split("//")[-1].split("/")[0].lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in _APPROVED_CITATION_DOMAINS)
    except Exception:
        return False


def save_article(article: dict, output_dir: Path) -> Path:
    """Save generated TypeScript to output/articles/<slug>.ts"""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = article["slug"]
    file_path = output_dir / f"{slug}.ts"
    file_path.write_text(article["ts_code"], encoding="utf-8")
    print(f"  [article_generator] Saved: {file_path}")
    return file_path
