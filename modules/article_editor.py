"""
Article editor module — bidirectional TypeScript Paper <-> Python dict parser/serializer.

Provides:
  parse_ts_to_dict(ts_code)        TS string -> Paper dict
  serialize_dict_to_ts(paper)      Paper dict -> TS string
  rename_article(old_slug, ...)    Rename slug + title + all associated files
  update_article_fields(slug, ...) Patch specific fields
  validate_paper_dict(paper)       Returns list of error strings
"""

import re
import json
from pathlib import Path


# ── Slug / camel helpers ────────────────────────────────────────────────────────

def _slug_to_camel(slug: str) -> str:
    parts = slug.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def generate_slug(title: str) -> str:
    """Derive a URL slug from a title (mirrors article_generator.py logic)."""
    title = title.split(":")[0].strip()
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s-]", "", title)
    title = title.strip()
    title = re.sub(r"\s+", "-", title)
    title = re.sub(r"-+", "-", title)
    parts = title.split("-")[:8]
    return "-".join(parts)


# ── Low-level string extraction ─────────────────────────────────────────────────

def _extract_scalar(ts_code: str, field: str) -> str:
    """
    Extract a single string field from TypeScript source.
    Handles single-quoted, double-quoted, and backtick (template literal) strings.
    Uses char-by-char scanning to correctly handle escaped quotes.
    The field: pattern may have a newline + whitespace before the quote.
    """
    pattern = re.compile(rf'(?<!["\w]){re.escape(field)}\s*:\s*')
    match = pattern.search(ts_code)
    if not match:
        return ""

    pos = match.end()
    # Skip any remaining whitespace including newlines
    while pos < len(ts_code) and ts_code[pos] in (' ', '\t', '\n', '\r'):
        pos += 1

    if pos >= len(ts_code):
        return ""

    quote = ts_code[pos]
    if quote not in ('"', "'", '`'):
        return ""

    pos += 1
    result = []
    while pos < len(ts_code):
        ch = ts_code[pos]
        if quote != '`' and ch == '\\':
            pos += 1
            if pos < len(ts_code):
                esc = ts_code[pos]
                if esc == 'n':
                    result.append('\n')
                elif esc == 't':
                    result.append('\t')
                elif esc == '"':
                    result.append('"')
                elif esc == "'":
                    result.append("'")
                elif esc == '\\':
                    result.append('\\')
                else:
                    result.append(esc)
        elif ch == quote:
            break
        else:
            result.append(ch)
        pos += 1

    return "".join(result)


def _extract_block(ts_code: str, field: str, open_char: str, close_char: str) -> str:
    """
    Find 'field: [' or 'field: {' (with optional whitespace/newlines) and
    extract the full block using depth counting.
    Returns the inner content (not including the outer open/close chars).
    Returns "" if not found.
    """
    pattern = re.compile(
        rf'(?<!["\w]){re.escape(field)}\s*:\s*{re.escape(open_char)}',
        re.DOTALL,
    )
    match = pattern.search(ts_code)
    if not match:
        return ""

    start = match.end()
    depth = 1
    i = start
    in_string = False
    string_char = ''
    while i < len(ts_code) and depth > 0:
        ch = ts_code[i]
        if in_string:
            if ch == '\\' and string_char != '`':
                i += 2
                continue
            elif ch == string_char:
                in_string = False
        elif ch in ('"', "'", '`'):
            in_string = True
            string_char = ch
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                break
        i += 1

    return ts_code[start:i]


def _extract_string_array_from_block(block: str) -> list[str]:
    """Extract string[] items from a raw block (without outer brackets)."""
    items = []
    i = 0
    while i < len(block):
        ch = block[i]
        if ch in ('"', "'", '`'):
            quote = ch
            i += 1
            val = []
            while i < len(block):
                c = block[i]
                if quote != '`' and c == '\\':
                    i += 1
                    if i < len(block):
                        esc = block[i]
                        if esc == 'n':
                            val.append('\n')
                        elif esc == 't':
                            val.append('\t')
                        else:
                            val.append(esc)
                elif c == quote:
                    break
                else:
                    val.append(c)
                i += 1
            items.append("".join(val))
        i += 1
    return items


def _extract_string_array(ts_code: str, field: str) -> list[str]:
    """Extract a string[] field from TypeScript source."""
    block = _extract_block(ts_code, field, '[', ']')
    if not block:
        return []
    return _extract_string_array_from_block(block)


def _split_objects(block: str) -> list[str]:
    """
    Split a block of '{ ... }, { ... }' into individual object strings
    by tracking { } depth. Correctly handles quoted strings.
    """
    objects = []
    depth = 0
    start = -1
    i = 0
    in_string = False
    string_char = ''
    while i < len(block):
        ch = block[i]
        if in_string:
            if ch == '\\' and string_char != '`':
                i += 2
                continue
            elif ch == string_char:
                in_string = False
        elif ch in ('"', "'", '`'):
            in_string = True
            string_char = ch
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(block[start:i + 1])
                start = -1
        i += 1
    return objects


def _parse_simple_object(obj_str: str, fields: list[str]) -> dict:
    """Parse a simple TypeScript object string — only string scalar fields."""
    return {f: _extract_scalar(obj_str, f) for f in fields}


def _parse_section_object(obj_str: str) -> dict:
    """Parse a single section TypeScript object into a Python dict."""
    sec_type = _extract_scalar(obj_str, 'type')
    if not sec_type:
        return {}

    section: dict = {"type": sec_type}

    if sec_type in ("h2", "h3", "p", "callout"):
        section["text"] = _extract_scalar(obj_str, 'text')

    elif sec_type in ("ul", "ol"):
        items_block = _extract_block(obj_str, 'items', '[', ']')
        section["items"] = _extract_string_array_from_block(items_block) if items_block else []

    elif sec_type == "table":
        headers_block = _extract_block(obj_str, 'headers', '[', ']')
        section["headers"] = _extract_string_array_from_block(headers_block) if headers_block else []

        # Rows is string[][] — scan for inner arrays
        rows_block = _extract_block(obj_str, 'rows', '[', ']')
        rows = []
        if rows_block:
            inner_depth = 0
            row_start = -1
            j = 0
            while j < len(rows_block):
                c = rows_block[j]
                if c == '[':
                    if inner_depth == 0:
                        row_start = j + 1
                    inner_depth += 1
                elif c == ']':
                    inner_depth -= 1
                    if inner_depth == 0 and row_start >= 0:
                        rows.append(_extract_string_array_from_block(rows_block[row_start:j]))
                        row_start = -1
                elif c in ('"', "'"):
                    q = c
                    j += 1
                    while j < len(rows_block):
                        if rows_block[j] == '\\':
                            j += 2
                            continue
                        if rows_block[j] == q:
                            break
                        j += 1
                j += 1
        section["rows"] = rows

        caption = _extract_scalar(obj_str, 'caption')
        if caption:
            section["caption"] = caption

    return section


# ── Public: parse ───────────────────────────────────────────────────────────────

def parse_ts_to_dict(ts_code: str) -> dict:
    """
    Parse a TypeScript Paper object string into a Python dict.
    Returns a dict matching the Paper schema with all fields as Python types.
    Optional fields (subtitle, updated, tags) return "" / [] on absence.
    """
    paper: dict = {}

    # Scalar fields
    for field in ["slug", "title", "subtitle", "category", "audience",
                  "published", "updated", "readTime", "region", "abstract"]:
        paper[field] = _extract_scalar(ts_code, field)

    # Simple string arrays
    for field in ["keyFindings", "shopImplications", "carrierImplications", "tags"]:
        paper[field] = _extract_string_array(ts_code, field)

    # Authors: [{ name: "...", title: "..." }]
    authors_block = _extract_block(ts_code, "authors", "[", "]")
    paper["authors"] = (
        [_parse_simple_object(o, ["name", "title"]) for o in _split_objects(authors_block)]
        if authors_block else []
    )

    # FAQ: [{ q: "...", a: "..." }]
    faq_block = _extract_block(ts_code, "faq", "[", "]")
    paper["faq"] = (
        [_parse_simple_object(o, ["q", "a"]) for o in _split_objects(faq_block)]
        if faq_block else []
    )

    # Citations: [{ label: "...", url?: "..." }]
    citations_block = _extract_block(ts_code, "citations", "[", "]")
    paper["citations"] = (
        [_parse_simple_object(o, ["label", "url"]) for o in _split_objects(citations_block)]
        if citations_block else []
    )

    # Sections: complex union type
    sections_block = _extract_block(ts_code, "sections", "[", "]")
    if sections_block:
        raw_sections = [_parse_section_object(o) for o in _split_objects(sections_block)]
        paper["sections"] = [s for s in raw_sections if s]
    else:
        paper["sections"] = []

    return paper


# ── Public: serialize ───────────────────────────────────────────────────────────

def _ts_str(s: str) -> str:
    """Serialize a Python string to a TypeScript double-quoted string literal."""
    escaped = (s
               .replace('\\', '\\\\')
               .replace('"', '\\"')
               .replace('\n', '\\n')
               .replace('\t', '\\t'))
    return f'"{escaped}"'


def _serialize_section(section: dict, indent: int = 4) -> str:
    """Serialize a section dict to a TypeScript object string."""
    pad = " " * indent
    s_type = section.get("type", "p")

    if s_type in ("h2", "h3"):
        return f'{pad}{{ type: "{s_type}", text: {_ts_str(section.get("text", ""))} }}'

    elif s_type in ("p", "callout"):
        text = _ts_str(section.get("text", ""))
        return f'{pad}{{\n{pad}  type: "{s_type}",\n{pad}  text: {text},\n{pad}}}'

    elif s_type in ("ul", "ol"):
        items = section.get("items", [])
        items_lines = "\n".join(f'{pad}    {_ts_str(it)},' for it in items)
        return (
            f'{pad}{{\n'
            f'{pad}  type: "{s_type}",\n'
            f'{pad}  items: [\n'
            f'{items_lines}\n'
            f'{pad}  ],\n'
            f'{pad}}}'
        )

    elif s_type == "table":
        headers = section.get("headers", [])
        rows = section.get("rows", [])
        caption = section.get("caption", "")

        headers_str = ", ".join(_ts_str(h) for h in headers)
        rows_lines = []
        for row in rows:
            cells = ", ".join(_ts_str(c) for c in row)
            rows_lines.append(f'{pad}    [{cells}],')
        rows_block = "\n".join(rows_lines)

        parts = [
            f'{pad}{{',
            f'{pad}  type: "table",',
            f'{pad}  headers: [{headers_str}],',
            f'{pad}  rows: [',
            rows_block,
            f'{pad}  ],',
        ]
        if caption:
            parts.append(f'{pad}  caption: {_ts_str(caption)},')
        parts.append(f'{pad}}}')
        return "\n".join(parts)

    else:
        text = _ts_str(section.get("text", ""))
        return f'{pad}{{\n{pad}  type: "{s_type}",\n{pad}  text: {text},\n{pad}}}'


def serialize_dict_to_ts(paper: dict) -> str:
    """
    Serialize a Python dict back to a valid TypeScript Paper object string.
    Produces the same style as the auto-generated papers (2-space indent, double quotes).
    """
    slug = paper.get("slug", "")
    camel = _slug_to_camel(slug)

    lines = [
        'import type { Paper } from "../types";',
        "",
        f"export const {camel}: Paper = {{",
    ]

    # Scalar fields — emit subtitle only if non-empty
    for field in ["slug", "title"]:
        lines.append(f"  {field}: {_ts_str(paper.get(field, ''))},")

    subtitle = paper.get("subtitle", "")
    if subtitle:
        lines.append(f"  subtitle:")
        lines.append(f"    {_ts_str(subtitle)},")

    for field in ["category", "audience"]:
        lines.append(f"  {field}: {_ts_str(paper.get(field, ''))},")

    # Authors
    lines.append("  authors: [")
    for a in paper.get("authors", []):
        lines.append(f"    {{ name: {_ts_str(a.get('name', ''))}, title: {_ts_str(a.get('title', ''))} }},")
    lines.append("  ],")

    # Dates + metadata
    lines.append(f"  published: {_ts_str(paper.get('published', ''))},")
    updated = paper.get("updated", "")
    if updated:
        lines.append(f"  updated: {_ts_str(updated)},")
    lines.append(f"  readTime: {_ts_str(paper.get('readTime', ''))},")
    lines.append(f"  region: {_ts_str(paper.get('region', ''))},")

    # Tags (optional)
    tags = paper.get("tags", [])
    if tags:
        lines.append("  tags: [")
        for t in tags:
            lines.append(f"    {_ts_str(t)},")
        lines.append("  ],")

    # Abstract
    lines.append("  abstract:")
    lines.append(f"    {_ts_str(paper.get('abstract', ''))},")

    # Key findings
    lines.append("  keyFindings: [")
    for kf in paper.get("keyFindings", []):
        lines.append(f"    {_ts_str(kf)},")
    lines.append("  ],")

    # Sections
    lines.append("  sections: [")
    for sec in paper.get("sections", []):
        lines.append(_serialize_section(sec, indent=4) + ",")
    lines.append("  ],")

    # Shop / carrier implications
    lines.append("  shopImplications: [")
    for si in paper.get("shopImplications", []):
        lines.append(f"    {_ts_str(si)},")
    lines.append("  ],")

    lines.append("  carrierImplications: [")
    for ci in paper.get("carrierImplications", []):
        lines.append(f"    {_ts_str(ci)},")
    lines.append("  ],")

    # FAQ
    lines.append("  faq: [")
    for item in paper.get("faq", []):
        lines.append("    {")
        lines.append(f"      q: {_ts_str(item.get('q', ''))},")
        lines.append(f"      a: {_ts_str(item.get('a', ''))},")
        lines.append("    },")
    lines.append("  ],")

    # Citations
    lines.append("  citations: [")
    for cit in paper.get("citations", []):
        lines.append("    {")
        lines.append(f"      label:")
        lines.append(f"        {_ts_str(cit.get('label', ''))},")
        url = cit.get("url", "")
        if url:
            lines.append(f"      url: {_ts_str(url)},")
        lines.append("    },")
    lines.append("  ],")

    lines.append("};")

    return "\n".join(lines)


# ── Public: validate ────────────────────────────────────────────────────────────

def validate_paper_dict(paper: dict) -> list[str]:
    """Validate a paper dict for structural completeness before serialization."""
    errors = []

    for field in ["slug", "title", "category", "audience", "published", "readTime", "region", "abstract"]:
        if not paper.get(field, "").strip():
            errors.append(f"Missing required field: '{field}'")

    if len(paper.get("keyFindings", [])) < 3:
        errors.append("keyFindings must have at least 3 items")
    if len(paper.get("sections", [])) < 3:
        errors.append("sections must have at least 3 items")
    if len(paper.get("faq", [])) < 3:
        errors.append("faq must have at least 3 items")
    if len(paper.get("citations", [])) < 3:
        errors.append("citations must have at least 3 items")
    if len(paper.get("authors", [])) < 1:
        errors.append("Must have at least 1 author")

    return errors


# ── Public: file operations ─────────────────────────────────────────────────────

def update_article_fields(slug: str, fields: dict, output_dir: Path) -> dict:
    """Patch specific fields of an article without full re-parse/serialize."""
    ts_path = output_dir / "articles" / f"{slug}.ts"
    if not ts_path.exists():
        return {"success": False, "slug": slug, "message": f"Article file not found: {ts_path}"}

    try:
        paper = parse_ts_to_dict(ts_path.read_text(encoding="utf-8"))
        paper.update(fields)
        ts_path.write_text(serialize_dict_to_ts(paper), encoding="utf-8")
        return {"success": True, "slug": slug, "message": "Article updated successfully"}
    except Exception as e:
        return {"success": False, "slug": slug, "message": f"Error updating article: {e}"}


def rename_article(old_slug: str, new_title: str, output_dir: Path) -> dict:
    """Rename an article's slug, title, and all associated files atomically."""
    ts_path = output_dir / "articles" / f"{old_slug}.ts"
    if not ts_path.exists():
        return {
            "success": False,
            "old_slug": old_slug,
            "new_slug": "",
            "message": f"Article file not found: {ts_path}",
        }

    new_slug = generate_slug(new_title)
    if not new_slug:
        return {"success": False, "old_slug": old_slug, "new_slug": "", "message": "Could not derive a valid slug from the new title"}

    # If slug is unchanged, just update the title field
    if new_slug == old_slug:
        result = update_article_fields(old_slug, {"title": new_title}, output_dir)
        return {**result, "old_slug": old_slug, "new_slug": old_slug}

    new_ts_path = output_dir / "articles" / f"{new_slug}.ts"
    if new_ts_path.exists():
        return {
            "success": False,
            "old_slug": old_slug,
            "new_slug": new_slug,
            "message": f"Target slug '{new_slug}' already exists — choose a different title",
        }

    try:
        paper = parse_ts_to_dict(ts_path.read_text(encoding="utf-8"))
        paper["slug"] = new_slug
        paper["title"] = new_title
        new_ts_path.write_text(serialize_dict_to_ts(paper), encoding="utf-8")

        # Rename associated files
        for ext, sub in [(".png", "images"), (".txt", "linkedin")]:
            old_f = output_dir / sub / f"{old_slug}{ext}"
            new_f = output_dir / sub / f"{new_slug}{ext}"
            if old_f.exists():
                old_f.rename(new_f)

        # Remove old .ts file
        ts_path.unlink()

        # Update published.json — remove old slug entry (re-publish required after rename)
        published_path = output_dir / "published.json"
        if published_path.exists():
            try:
                registry = json.loads(published_path.read_text(encoding="utf-8"))
                if old_slug in registry:
                    del registry[old_slug]
                    published_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Update known_papers.json (may be in project root)
        for kp_path in [output_dir / "known_papers.json", Path("known_papers.json")]:
            if kp_path.exists():
                try:
                    known = json.loads(kp_path.read_text(encoding="utf-8"))
                    if isinstance(known, list):
                        for i, entry in enumerate(known):
                            if isinstance(entry, dict) and entry.get("slug") == old_slug:
                                known[i] = {**entry, "slug": new_slug, "title": new_title}
                    elif isinstance(known, dict) and old_slug in known:
                        known[new_slug] = known.pop(old_slug)
                    kp_path.write_text(json.dumps(known, indent=2), encoding="utf-8")
                except Exception:
                    pass
                break

        return {
            "success": True,
            "old_slug": old_slug,
            "new_slug": new_slug,
            "message": f"Renamed '{old_slug}' → '{new_slug}' successfully",
        }

    except Exception as e:
        # Clean up partial writes
        if new_ts_path.exists():
            new_ts_path.unlink()
        return {
            "success": False,
            "old_slug": old_slug,
            "new_slug": new_slug,
            "message": f"Rename failed: {e}",
        }
