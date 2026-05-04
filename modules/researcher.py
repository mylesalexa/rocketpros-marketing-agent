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

# Rotating topic category seeds — one random subset is queried each daily run
TOPIC_CATEGORIES = [
    "MPI accredited repair program requirements 2026",
    "SGI RPS scoring collision shop tier 2026",
    "ADAS calibration OEM requirements Canada collision",
    "collision repair cycle time reduction Canada 2026",
    "parts inflation collision repair Canada 2026",
    "Mitchell estimating best practices collision Canada",
    "collision repair documentation insurer approval Canada",
    "OEM position statements repair standard of care 2026",
    "supplement discipline collision repair best practices",
    "Canadian collision severity trends 2026",
    "aluminum collision repair MPI SGI requirements",
    "pre-scan post-scan required collision repair Canada",
    "OEM sectioning restrictions modern vehicles 2026",
    "labour rate vs labour hours collision repair Canada",
    "rental car cost reduction collision repair Canada",
    "ADAS recalibration windshield replacement MPI SGI",
    "how to write supplement approved first time Canada",
    "collision shop documentation workflow software 2026",
    "EV collision repair requirements Canada insurance",
    "structural scan collision repair MPI documentation",
]

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


def _build_topic_pitch(query: str, results: list[dict]) -> dict | None:
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

    return {
        "query": query,
        "title": _improve_title(query),
        "angle": f"Data-driven analysis for Canadian shops and insurers: {descriptions[:200]}",
        "audience": audience,
        "source_urls": source_urls[:5],
        "top_result_title": top_title,
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


def discover_topics(n_topics: int = 5) -> list[dict]:
    """
    Run Brave Search on a random subset of TOPIC_CATEGORIES and return
    n_topics unique, non-duplicate topic pitches ranked by relevance.

    Each returned topic dict has:
        - title: str
        - angle: str
        - audience: str
        - query: str
        - source_urls: list[str]
    """
    print(f"[researcher] Discovering topics via Brave Search ({QUERIES_PER_RUN} queries)...")

    # Pick a random subset of categories to query this run
    queries = random.sample(TOPIC_CATEGORIES, min(QUERIES_PER_RUN, len(TOPIC_CATEGORIES)))

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

    # Return top N (already in random order from sample, so just slice)
    return unique_topics[:n_topics]
