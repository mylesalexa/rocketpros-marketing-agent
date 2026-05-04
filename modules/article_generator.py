"""
Article generator — uses Claude claude-opus-4-7 with prompt caching to produce
Paper TypeScript objects matching /lib/research/types.ts exactly.

Prompt caching strategy:
  - System prompt + Paper type definition: cached (stable across runs)
  - Per-topic user message: not cached (changes each call)

Estimated cost per article: ~$0.08–0.15 with caching enabled.
"""

import os
import re
import json
from datetime import datetime
from pathlib import Path

import anthropic

from templates.paper_schema import validate_paper_dict


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")

SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "templates" / "system_prompt.txt"

# The full Paper TypeScript type definition — injected into the cached system context
# so the model has a precise schema reference without re-sending it every call.
PAPER_TYPE_DEFINITION = '''
// Full TypeScript type the output must match exactly:

export type Author = {
  name: string;
  role: string;
  company?: string;
};

export type FAQ = {
  question: string;
  answer: string;
};

export type Citation = {
  id: number;
  text: string;
  url?: string;
};

export type Section =
  | { type: "h2"; text: string; id?: string }
  | { type: "h3"; text: string; id?: string }
  | { type: "p"; text: string }
  | { type: "ul"; items: string[] }
  | { type: "ol"; items: string[] }
  | { type: "table"; headers: string[]; rows: string[][]; caption?: string }
  | { type: "callout"; text: string };

export type Paper = {
  slug: string;
  title: string;
  subtitle?: string;
  category: string;
  audience: string;
  authors: Author[];
  published: string;      // ISO date: "2026-05-04"
  updated?: string;
  readTime: string;       // e.g., "13 min"
  region: "Canada" | "United States" | "North America";
  abstract: string;
  keyFindings: string[];
  sections: Section[];
  shopImplications: string[];
  carrierImplications: string[];
  faq: FAQ[];
  citations: Citation[];
  tags?: string[];
};
'''


def _load_system_prompt() -> str:
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _slug_to_camel(slug: str) -> str:
    """Convert 'adas-calibration-mpi' → 'adasCalibrationMpi'"""
    parts = slug.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _generate_slug(title: str) -> str:
    """Convert a title to a URL-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    # Truncate to reasonable length
    parts = slug.split("-")
    if len(parts) > 8:
        slug = "-".join(parts[:8])
    return slug


def _extract_typescript_from_response(text: str) -> str:
    """
    Extract the TypeScript code from Claude's response.
    Handles cases where the model might wrap in markdown despite instructions.
    """
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```typescript or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)
    return text.strip()


def _extract_slug_from_typescript(ts_code: str) -> str | None:
    """Extract the slug value from generated TypeScript."""
    match = re.search(r'slug:\s*["\']([^"\']+)["\']', ts_code)
    if match:
        return match.group(1)
    return None


def _extract_title_from_typescript(ts_code: str) -> str | None:
    """Extract the title value from generated TypeScript."""
    match = re.search(r'title:\s*["\']([^"\']+)["\']', ts_code)
    if match:
        return match.group(1)
    return None


def generate_article(topic: dict) -> dict:
    """
    Generate a full Paper TypeScript object for the given topic.

    Args:
        topic: dict with keys: title, angle, audience, source_urls

    Returns:
        dict with keys:
            - ts_code: str (full TypeScript file content, ready to paste)
            - slug: str
            - title: str
            - validation_errors: list[str]
            - token_usage: dict
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = _load_system_prompt()
    today = datetime.now().strftime("%Y-%m-%d")
    topic_title = topic.get("title", "Unknown Topic")
    topic_angle = topic.get("angle", "")
    topic_audience = topic.get("audience", "Collision Repair Shops + Insurers")
    source_urls = topic.get("source_urls", [])

    suggested_slug = _generate_slug(topic_title)
    camel_slug = _slug_to_camel(suggested_slug)

    source_context = ""
    if source_urls:
        source_context = f"\n\nResearch sources found during topic discovery (use for citations where relevant):\n"
        source_context += "\n".join(f"- {url}" for url in source_urls[:5])

    user_message = f"""Generate a complete RocketPros research article as a TypeScript Paper object.

TOPIC: {topic_title}
ANGLE: {topic_angle}
PRIMARY AUDIENCE: {topic_audience}
PUBLISHED DATE: {today}
SUGGESTED SLUG: {suggested_slug}
CAMELCASE EXPORT NAME: {camel_slug}
SITE URL: {SITE_URL}{source_context}

Requirements:
- Output ONLY the TypeScript file (no prose, no markdown fences)
- Start with: import {{ Paper }} from "@/lib/research/types";
- Export name must be: {camel_slug}
- slug must be: {suggested_slug}
- published must be: "{today}"
- region must be: "Canada"
- Include exactly 2 authors: Myles Chaput (CEO, RocketPros) and Ali Jakvani (Head of Product, RocketPros)
- readTime format: "X min" (calculate based on ~200 words/min for 2000–2500 word article)
- All H2 section headings must be questions
- Include minimum 2 tables, 1 callout, 5 FAQ items, 8 citations
- shopImplications: 4–6 items, carrierImplications: 3–5 items
- keyFindings: 4–6 items, each starting with a number or percentage

Generate the full TypeScript file now:"""

    print(f"  [article_generator] Generating article: '{topic_title}'")

    # Use streaming with prompt caching on the system content
    # The system prompt + type definition are stable → cached after first call
    full_response = ""
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0

    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": system_prompt + "\n\n" + PAPER_TYPE_DEFINITION,
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
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0)
        cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0)

    ts_code = _extract_typescript_from_response(full_response)
    slug = _extract_slug_from_typescript(ts_code) or suggested_slug
    title = _extract_title_from_typescript(ts_code) or topic_title

    # Light validation — parse the section types from the raw TypeScript
    # (Full AST parsing would require a TS parser; we do regex-level checks)
    validation_errors = _validate_typescript_output(ts_code)

    token_usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }

    print(f"  [article_generator] Done. Tokens: {input_tokens} in / {output_tokens} out "
          f"({cache_read_tokens} cache read, {cache_write_tokens} cache write)")

    if validation_errors:
        print(f"  [article_generator] Validation warnings: {validation_errors}")

    return {
        "ts_code": ts_code,
        "slug": slug,
        "title": title,
        "validation_errors": validation_errors,
        "token_usage": token_usage,
        "topic": topic,
    }


def _validate_typescript_output(ts_code: str) -> list[str]:
    """
    Regex-level validation of the generated TypeScript.
    Returns list of warning strings.
    """
    errors = []

    if 'import { Paper }' not in ts_code and "import { Paper }" not in ts_code:
        errors.append("Missing: import { Paper } from '@/lib/research/types'")

    if 'export const' not in ts_code:
        errors.append("Missing: export const <name>: Paper = {...}")

    table_count = ts_code.count('"type": "table"') + ts_code.count("type: \"table\"") + ts_code.count("type: 'table'")
    if table_count < 2:
        errors.append(f"Only {table_count} table(s) found — need at least 2")

    faq_count = len(re.findall(r'question:', ts_code))
    if faq_count < 5:
        errors.append(f"Only {faq_count} FAQ question(s) found — need at least 5")

    citation_count = len(re.findall(r'id:\s*\d+', ts_code))
    if citation_count < 8:
        errors.append(f"Only {citation_count} citation(s) found — need at least 8")

    if "Myles Chaput" not in ts_code:
        errors.append("Missing author: Myles Chaput")

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
