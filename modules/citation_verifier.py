"""
Citation verifier — spot-checks that citation URLs in generated articles are
reachable and belong to approved high-authority domains.

Checks up to MAX_CITATIONS_TO_CHECK per article using HEAD requests (fast).
Results are non-fatal: dead or untrusted URLs are flagged for human review in
the email digest but do not block publishing.
"""

import re
import httpx


MAX_CITATIONS_TO_CHECK = 5
REQUEST_TIMEOUT = 6.0  # seconds per URL

# Approved domains — mirrors APPROVED_DOMAINS in researcher.py
APPROVED_DOMAINS = {
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


def _is_approved_domain(url: str) -> bool:
    try:
        host = url.lower().split("//")[-1].split("/")[0].lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in APPROVED_DOMAINS)
    except Exception:
        return False


def _check_url(url: str, client: httpx.Client) -> str:
    """
    HEAD-request a URL. Returns "ok", "dead", or "error".
    Falls back to GET if server returns 405 Method Not Allowed.
    """
    try:
        resp = client.head(url, follow_redirects=True, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 405:
            # Some servers reject HEAD — try GET with streaming off
            resp = client.get(url, follow_redirects=True, timeout=REQUEST_TIMEOUT)
        return "ok" if resp.status_code < 400 else "dead"
    except httpx.TimeoutException:
        return "error"
    except Exception:
        return "error"


def _extract_citation_urls(ts_code: str) -> list[str]:
    """Pull all url: '...' values from the citations array of a TypeScript file."""
    # Only look inside the citations block to avoid false positives from other fields
    citations_block_match = re.search(r'citations:\s*\[(.+?)\]', ts_code, re.DOTALL)
    if not citations_block_match:
        return []
    block = citations_block_match.group(1)
    return re.findall(r'url:\s*["\']([^"\']+)["\']', block)


def verify_citations(ts_code: str) -> list[dict]:
    """
    Spot-check up to MAX_CITATIONS_TO_CHECK citation URLs from a generated article.

    Args:
        ts_code: Full TypeScript file content of the generated article.

    Returns:
        List of dicts: { url, trusted, status }
        status: "ok" | "dead" | "untrusted" | "error" | "skipped"
    """
    urls = _extract_citation_urls(ts_code)
    if not urls:
        return []

    to_check = urls[:MAX_CITATIONS_TO_CHECK]
    results = []

    print(f"  [citation_verifier] Checking {len(to_check)} citation URL(s)...")

    with httpx.Client(
        headers={"User-Agent": "RocketPros-CitationVerifier/1.0"},
        follow_redirects=True,
    ) as client:
        for url in to_check:
            trusted = _is_approved_domain(url)
            if not trusted:
                # Still check reachability, but flag as untrusted domain
                status = _check_url(url, client)
                final_status = "untrusted" if status == "ok" else status
            else:
                final_status = _check_url(url, client)

            icon = "✓" if final_status == "ok" else "⚠ " if final_status == "untrusted" else "✗"
            domain_note = " [approved]" if trusted else " [unknown domain]"
            print(f"    {icon} {url}{domain_note} → {final_status}")

            results.append({"url": url, "trusted": trusted, "status": final_status})

    # Mark any remaining unchecked URLs as skipped
    for url in urls[MAX_CITATIONS_TO_CHECK:]:
        results.append({"url": url, "trusted": _is_approved_domain(url), "status": "skipped"})

    return results


def summarize_verification(results: list[dict]) -> dict:
    """
    Summarize citation verification results into counts and quality flags.

    Returns:
        {
            "checked": int,
            "ok": int,
            "dead": int,
            "untrusted": int,
            "error": int,
            "quality_flags": list[str],
        }
    """
    checked = [r for r in results if r["status"] != "skipped"]
    counts = {
        "checked": len(checked),
        "ok": sum(1 for r in checked if r["status"] == "ok"),
        "dead": sum(1 for r in checked if r["status"] == "dead"),
        "untrusted": sum(1 for r in checked if r["status"] == "untrusted"),
        "error": sum(1 for r in checked if r["status"] == "error"),
        "quality_flags": [],
    }

    dead_urls = [r["url"] for r in checked if r["status"] == "dead"]
    if dead_urls:
        counts["quality_flags"].append(
            f"CITATION_WARN: {len(dead_urls)} dead URL(s) — verify before publishing: {dead_urls}"
        )

    untrusted_urls = [r["url"] for r in checked if r["status"] == "untrusted"]
    if untrusted_urls:
        counts["quality_flags"].append(
            f"CITATION_WARN: {len(untrusted_urls)} citation(s) from non-approved domains: {untrusted_urls}"
        )

    return counts
