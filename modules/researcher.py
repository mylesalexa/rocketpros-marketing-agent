"""
Research module — discovers trending collision repair topics using Brave Search API.
Returns ranked topic pitches ready for article generation.
"""

import os
import random
import httpx
from datetime import datetime

from modules.deduplicator import filter_topics


BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# Topic categories organized by tier for balanced daily runs.
# Each entry: (query_string, tier)
# tier: "niche" = MPI/SGI/Canadian programs, "canada" = pan-Canadian, "north_america" = US or NA
TOPIC_CATEGORIES_TIERED = [
    # ── Niche: MPI / SGI (Manitoba + Saskatchewan) ─────────────────────────────
    ("MPI accredited repair program requirements 2026", "niche"),
    ("SGI RPS scoring collision shop tier 2026", "niche"),
    ("ADAS calibration OEM requirements Canada collision", "niche"),
    ("collision repair cycle time reduction Canada 2026", "niche"),
    ("parts inflation collision repair Canada 2026", "niche"),
    ("Mitchell estimating best practices collision Canada", "niche"),
    ("collision repair documentation insurer approval Canada", "niche"),
    ("OEM position statements repair standard of care 2026", "niche"),
    ("supplement discipline collision repair best practices", "niche"),
    ("Canadian collision severity trends 2026", "niche"),
    ("aluminum collision repair MPI SGI requirements", "niche"),
    ("pre-scan post-scan required collision repair Canada", "niche"),
    ("OEM sectioning restrictions modern vehicles 2026", "niche"),
    ("labour rate vs labour hours collision repair Canada", "niche"),
    ("rental car cost reduction collision repair Canada", "niche"),
    ("ADAS recalibration windshield replacement MPI SGI", "niche"),
    ("how to write supplement approved first time Canada", "niche"),
    ("collision shop documentation workflow software 2026", "niche"),
    ("EV collision repair requirements Canada insurance", "niche"),
    ("structural scan collision repair MPI documentation", "niche"),
    # ── Canadian-broad (pan-Canadian, not MPI/SGI specific) ────────────────────
    ("ICBC material damage program accredited shop 2026", "canada"),
    ("Intact DRP program collision shop documentation Canada", "canada"),
    ("CCIF collision repair industry trends Canada 2026", "canada"),
    ("Canadian collision repair parts inflation IBC 2026", "canada"),
    ("EV collision repair battery assessment insurance Canada", "canada"),
    ("hail damage repair program catastrophe response Canada collision", "canada"),
    # ── North American / US ────────────────────────────────────────────────────
    ("State Farm Select Service DRP documentation requirements 2026", "north_america"),
    ("GEICO ARX program collision shop cycle time 2026", "north_america"),
    ("Progressive Service Center collision repair requirements 2026", "north_america"),
    ("ADAS calibration requirements DRP collision repair US 2026", "north_america"),
    ("Assured Performance OEM certification collision repair 2026", "north_america"),
    ("CCC ONE estimating best practices collision repair 2026", "north_america"),
    ("supplement approval rate collision repair US shops 2026", "north_america"),
    ("collision repair severity United States 2026 CCC Crash Course", "north_america"),
    ("SCRS collision repair industry study findings 2026", "north_america"),
    ("non-OEM aftermarket parts DRP insurer requirements 2026", "north_america"),
    ("EV collision repair Tesla Rivian OEM certification requirements", "north_america"),
    ("cycle time benchmark DRP collision repair United States 2026", "north_america"),
    ("pre-scan post-scan required collision repair United States 2026", "north_america"),
    ("OEM position statements structural repair North America 2026", "north_america"),
]

# Flat list for backward-compatible use
TOPIC_CATEGORIES = [q for q, _ in TOPIC_CATEGORIES_TIERED]

# How many search queries to run per daily pipeline invocation
QUERIES_PER_RUN = 6


def _brave_search(query: str, count: int = 5) -> list[dict]:
    """
    Execute a single Brave Search query.
    Returns a list of result dicts with keys: title, url, description.
    """
    if not BRAVE_API_KEY:
        raise RuntimeError("BRAVE_API_KEY environment variable is not set.")

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {
        "q": query,
        "count": count,
        "search_lang": "en",
        "country": "CA",
        "text_decorations": False,
        "safesearch": "moderate",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(BRAVE_SEARCH_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        web_results = data.get("web", {}).get("results", [])
        for r in web_results:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
            })
        return results

    except httpx.HTTPError as e:
        print(f"  [researcher] Brave Search error for '{query}': {e}")
        return []


def _build_topic_pitch(query: str, results: list[dict], direction: str = "") -> dict | None:
    """
    Convert a query + search results into a structured topic pitch.
    Returns None if the results are too thin to generate a pitch.
    """
    if not results:
        return None

    # Use the top result title as a signal, but craft a better article title
    top_title = results[0].get("title", query)
    descriptions = " ".join(r.get("description", "") for r in results[:3])

    # Determine likely audience from query keywords
    audience = "Collision Repair Shops + Insurers"
    if any(kw in query.lower() for kw in ["shop", "estimat", "supplement", "docum", "aluminum", "repair"]):
        audience = "Collision Repair Shops"
    elif any(kw in query.lower() for kw in ["adjuster", "insurer", "carrier", "mpi program", "sgi program"]):
        audience = "MPI/SGI Adjusters + Program Managers"

    # Extract source URLs for potential citations
    source_urls = [r["url"] for r in results if r.get("url")]

    # Build angle — incorporate direction if provided
    angle_prefix = f"Directed focus: {direction}. " if direction else ""
    angle = f"{angle_prefix}Data-driven analysis for Canadian shops and insurers: {descriptions[:200]}"

    return {
        "query": query,
        "title": _improve_title(direction if direction else query),
        "angle": angle,
        "audience": audience,
        "source_urls": source_urls[:5],
        "top_result_title": top_title,
        "direction": direction,
    }


def _improve_title(query: str) -> str:
    """
    Convert a raw search query into a proper article title.
    Keeps it Canada/MPI/SGI specific.
    """
    year = datetime.now().year
    # Capitalize and clean
    title = query.strip().title()
    # Remove trailing year if it's already in the query
    title = title.replace(str(year), "").strip().rstrip(",").strip()
    # Add year for freshness signals
    if str(year) not in title:
        title = f"{title}: A {year} Guide for Canadian Shops"
    return title


def discover_topics(n_topics: int = 5, direction: str = "") -> list[dict]:
    """
    Run Brave Search and return n_topics unique, deduplicated topic pitches.

    If `direction` is provided, it is used as the primary search focus —
    the agent searches specifically for that topic and builds articles around it.
    If blank, a random subset of TOPIC_CATEGORIES is used (autonomous mode).

    Each returned topic dict has:
        - title: str
        - angle: str
        - audience: str
        - query: str
        - source_urls: list[str]
        - direction: str  (original direction hint, passed through to article generator)
    """
    if direction.strip():
        return _discover_from_direction(direction.strip(), n_topics)
    else:
        return _discover_autonomous(n_topics)


def _discover_autonomous(n_topics: int) -> list[dict]:
    """Autonomous mode: guaranteed mix of niche + north_america + canada queries."""
    print(f"[researcher] Autonomous mode — discovering via Brave Search ({QUERIES_PER_RUN} queries)...")

    niche_pool = [q for q, t in TOPIC_CATEGORIES_TIERED if t == "niche"]
    na_pool = [q for q, t in TOPIC_CATEGORIES_TIERED if t == "north_america"]
    canada_pool = [q for q, t in TOPIC_CATEGORIES_TIERED if t == "canada"]

    # Always pick at least 1 niche and 1 north_america; fill remainder randomly from all
    guaranteed = [
        random.choice(niche_pool),
        random.choice(na_pool),
    ]
    remaining_pool = [q for q in TOPIC_CATEGORIES if q not in guaranteed]
    remaining_count = max(0, QUERIES_PER_RUN - len(guaranteed))
    remaining = random.sample(remaining_pool, min(remaining_count, len(remaining_pool)))
    queries = guaranteed + remaining
    random.shuffle(queries)

    raw_topics = []
    for query in queries:
        print(f"  Searching: {query}")
        results = _brave_search(query, count=5)
        pitch = _build_topic_pitch(query, results)
        if pitch:
            raw_topics.append(pitch)

    print(f"[researcher] Got {len(raw_topics)} raw topics, running deduplication...")
    unique_topics = filter_topics(raw_topics)
    print(f"[researcher] {len(unique_topics)} unique topics after dedup")
    return unique_topics[:n_topics]


def _direction_to_search_query(direction: str) -> str:
    """
    Distill a (potentially long, multi-line) direction into a short Brave Search
    query. Brave rejects queries with newlines or over ~500 chars (422 error).
    Strategy: take the first non-empty line, strip markdown, cap at 120 chars.
    """
    first_line = next(
        (l.strip() for l in direction.splitlines() if l.strip()),
        direction,
    )
    # Strip markdown bold/italic markers
    import re as _re
    first_line = _re.sub(r"\*+", "", first_line).strip()
    # Cap length so Brave doesn't reject it
    if len(first_line) > 120:
        first_line = first_line[:120].rsplit(" ", 1)[0]
    return first_line


def _discover_from_direction(direction: str, n_topics: int) -> list[dict]:
    """
    Directed mode: search specifically for the user-provided direction,
    then expand with 1–2 closely related queries to fill out n_topics.
    """
    print(f"[researcher] Directed mode — focus: '{direction}'")

    # Distill direction into a short search query (full direction causes Brave 422)
    base_query = _direction_to_search_query(direction)
    print(f"[researcher] Search query: '{base_query}'")

    queries = [
        base_query,
        f"{base_query} MPI SGI Canada collision repair",
        f"{base_query} Canadian auto insurance accredited shop 2026",
    ][:max(2, n_topics + 1)]

    raw_topics = []
    for query in queries:
        print(f"  Searching: {query}")
        results = _brave_search(query, count=8)  # More results for directed queries
        pitch = _build_topic_pitch(query, results, direction=direction)
        if pitch:
            raw_topics.append(pitch)

    print(f"[researcher] Got {len(raw_topics)} raw topics, running deduplication...")
    unique_topics = filter_topics(raw_topics)
    print(f"[researcher] {len(unique_topics)} unique topics after dedup")

    if not unique_topics:
        # If dedup filtered everything (topic already covered), still allow it with a warning
        print(f"[researcher] ⚠ All topics filtered by dedup — allowing directed topic through anyway")
        if raw_topics:
            raw_topics[0]["direction"] = direction
            unique_topics = raw_topics[:1]

    return unique_topics[:n_topics]
