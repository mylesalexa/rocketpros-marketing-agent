"""
Deduplication module — prevents generating articles too similar to existing papers.
Uses TF-IDF cosine similarity on titles to catch near-duplicates.
"""

import json
import re
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


KNOWN_PAPERS_PATH = Path(__file__).parent.parent / "known_papers.json"
# Reject topics with cosine similarity above this threshold vs. any existing paper
SIMILARITY_THRESHOLD = 0.55


def _load_known_papers() -> list[dict]:
    with open(KNOWN_PAPERS_PATH, "r") as f:
        data = json.load(f)
    return data["papers"]


def _slugify(text: str) -> str:
    """Convert a title to a URL-safe slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text


def is_duplicate(candidate_title: str, threshold: float = SIMILARITY_THRESHOLD) -> tuple[bool, str]:
    """
    Check if a candidate topic title is too similar to any existing paper.

    Returns:
        (is_duplicate: bool, reason: str)
        is_duplicate=True means the topic should be rejected.
    """
    known = _load_known_papers()
    existing_titles = [p["title"] for p in known]

    if not existing_titles:
        return False, ""

    # Vectorize all titles together
    all_titles = existing_titles + [candidate_title]
    try:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
        tfidf = vectorizer.fit_transform(all_titles)
    except ValueError:
        # Vocabulary too small (e.g., single-word titles) — fall back to exact match
        candidate_lower = candidate_title.lower()
        for t in existing_titles:
            if t.lower() == candidate_lower:
                return True, f"Exact match: '{t}'"
        return False, ""

    candidate_vec = tfidf[-1]
    existing_vecs = tfidf[:-1]
    similarities = cosine_similarity(candidate_vec, existing_vecs)[0]

    max_idx = int(np.argmax(similarities))
    max_score = float(similarities[max_idx])

    if max_score >= threshold:
        matched_title = existing_titles[max_idx]
        return True, f"Too similar to existing paper '{matched_title}' (score: {max_score:.2f})"

    return False, ""


def filter_topics(candidates: list[dict], threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    """
    Filter a list of topic dicts (each with a 'title' key) to remove near-duplicates.
    Returns only unique candidates, preserving order.
    """
    accepted = []
    # Build a running list of titles to also check candidates against each other
    running_titles = [p["title"] for p in _load_known_papers()]

    for candidate in candidates:
        title = candidate.get("title", "")
        if not title:
            continue

        # Check against known papers + previously accepted candidates
        all_known = running_titles[:]
        if all_known:
            try:
                vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
                all_titles = all_known + [title]
                tfidf = vectorizer.fit_transform(all_titles)
                candidate_vec = tfidf[-1]
                existing_vecs = tfidf[:-1]
                sims = cosine_similarity(candidate_vec, existing_vecs)[0]
                max_score = float(np.max(sims))
                if max_score >= threshold:
                    print(f"  [dedup] Rejected '{title}' — similarity {max_score:.2f}")
                    continue
            except ValueError:
                pass  # Not enough vocabulary — accept it

        accepted.append(candidate)
        running_titles.append(title)

    return accepted


def add_paper_to_known(slug: str, title: str) -> None:
    """
    Persist a newly generated paper to known_papers.json so future runs skip it.
    """
    known = _load_known_papers()
    # Don't add duplicates
    if not any(p["slug"] == slug for p in known):
        known.append({"slug": slug, "title": title})
        data = {"papers": known}
        with open(KNOWN_PAPERS_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  [dedup] Added '{slug}' to known_papers.json")


def get_known_slugs() -> list[str]:
    return [p["slug"] for p in _load_known_papers()]
