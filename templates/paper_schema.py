"""
Python mirror of the Paper TypeScript type from /lib/research/types.ts.
IMPORTANT: Field names must match the TypeScript types exactly.

Key differences from naive assumptions:
  - Author uses "title" not "role"
  - FAQ uses "q" and "a" not "question" and "answer"
  - Citation uses "label" not "text", and has no "id" field
"""

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class Author:
    name: str
    title: str      # e.g., "Co-founder, RocketPros" — NOT "role"


@dataclass
class FAQ:
    q: str          # NOT "question"
    a: str          # NOT "answer"


@dataclass
class Citation:
    label: str      # Full descriptive citation string — NOT "text", NO "id"
    url: Optional[str] = None


# Section types mirror the TypeScript discriminated union
@dataclass
class SectionH2:
    type: Literal["h2"] = "h2"
    text: str = ""
    id: Optional[str] = None


@dataclass
class SectionH3:
    type: Literal["h3"] = "h3"
    text: str = ""
    id: Optional[str] = None


@dataclass
class SectionP:
    type: Literal["p"] = "p"
    text: str = ""


@dataclass
class SectionUL:
    type: Literal["ul"] = "ul"
    items: list[str] = field(default_factory=list)


@dataclass
class SectionOL:
    type: Literal["ol"] = "ol"
    items: list[str] = field(default_factory=list)


@dataclass
class SectionTable:
    type: Literal["table"] = "table"
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    caption: Optional[str] = None


@dataclass
class SectionCallout:
    type: Literal["callout"] = "callout"
    text: str = ""


Section = SectionH2 | SectionH3 | SectionP | SectionUL | SectionOL | SectionTable | SectionCallout


@dataclass
class Paper:
    slug: str
    title: str
    category: str
    audience: str
    authors: list[Author]
    published: str      # ISO date string: "2026-05-04"
    readTime: str       # e.g., "13 min read" (include "read")
    region: Literal["Canada", "United States", "North America"]
    abstract: str
    keyFindings: list[str]
    sections: list[Section]
    shopImplications: list[str]
    carrierImplications: list[str]
    faq: list[FAQ]
    citations: list[Citation]
    subtitle: Optional[str] = None
    updated: Optional[str] = None
    tags: Optional[list[str]] = None


# Validation helpers

def validate_paper_dict(paper: dict) -> list[str]:
    """
    Validate a parsed paper dictionary against minimum AEO requirements.
    Returns a list of error strings (empty = valid).
    """
    errors = []

    required_keys = [
        "slug", "title", "category", "audience", "authors", "published",
        "readTime", "region", "abstract", "keyFindings", "sections",
        "shopImplications", "carrierImplications", "faq", "citations"
    ]
    for key in required_keys:
        if key not in paper:
            errors.append(f"Missing required field: {key}")

    # Check author schema
    if "authors" in paper:
        for i, author in enumerate(paper["authors"]):
            if "title" not in author:
                errors.append(f"authors[{i}] missing 'title' field (not 'role')")
            if "role" in author:
                errors.append(f"authors[{i}] has 'role' — should be 'title'")

    # Check FAQ schema
    if "faq" in paper:
        for i, faq in enumerate(paper["faq"]):
            if "q" not in faq:
                errors.append(f"faq[{i}] missing 'q' field (not 'question')")
            if "a" not in faq:
                errors.append(f"faq[{i}] missing 'a' field (not 'answer')")
            if "question" in faq:
                errors.append(f"faq[{i}] has 'question' — should be 'q'")
            if "answer" in faq:
                errors.append(f"faq[{i}] has 'answer' — should be 'a'")

    # Check citation schema
    if "citations" in paper:
        for i, citation in enumerate(paper["citations"]):
            if "label" not in citation:
                errors.append(f"citations[{i}] missing 'label' field (not 'text')")
            if "text" in citation:
                errors.append(f"citations[{i}] has 'text' — should be 'label'")
            if "id" in citation:
                errors.append(f"citations[{i}] has 'id' — citations have no id field")

    if "keyFindings" in paper and len(paper["keyFindings"]) < 3:
        errors.append("keyFindings must have at least 3 items")

    if "faq" in paper and len(paper["faq"]) < 5:
        errors.append("faq must have at least 5 questions")

    if "citations" in paper and len(paper["citations"]) < 7:
        errors.append("citations must have at least 7 entries")

    if "sections" in paper:
        tables = [s for s in paper["sections"] if s.get("type") == "table"]
        if len(tables) < 2:
            errors.append(f"sections must include at least 2 tables (AEO requirement), found {len(tables)}")

        h2s = [s for s in paper["sections"] if s.get("type") == "h2"]
        if len(h2s) < 5:
            errors.append(f"sections must include at least 5 H2 headings, found {len(h2s)}")

    if "region" in paper and paper["region"] not in ("Canada", "United States", "North America"):
        errors.append(f"region must be 'Canada', 'United States', or 'North America', got: {paper['region']}")

    return errors
