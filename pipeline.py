"""
Pipeline orchestrator — runs the full daily marketing agent workflow.

Steps:
  1. Discover topics via Brave Search
  2. Generate articles (Claude claude-opus-4-7 + prompt caching)
  3. Generate LinkedIn posts (3 variants per article)
  4. Generate hero images (DALL-E 3)
  5. Send email digest (Resend)
  6. Persist new paper slugs to known_papers.json
"""

import os
import sys
import json
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from modules.researcher import discover_topics
from modules.article_generator import generate_article, save_article
from modules.linkedin_generator import generate_linkedin_posts, save_linkedin_posts
from modules.image_generator import generate_image, save_image
from modules.email_digest import send_digest
from modules.deduplicator import add_paper_to_known


OUTPUT_DIR = Path(__file__).parent / "output"
ARTICLES_PER_RUN = int(os.getenv("ARTICLES_PER_RUN", "2"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def run_pipeline(dry_run: bool = False, direction: str = "") -> dict:
    """
    Execute the full daily pipeline.

    Args:
        dry_run: If True, skips email send and known_papers update.
        direction: Optional topic direction from the dashboard. If provided,
                   the researcher focuses on this specific subject instead of
                   picking random topics autonomously.

    Returns a summary dict with run statistics.
    """
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"RocketPros Marketing Agent — {'Directed' if direction else 'Autonomous'} Run")
    print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S CT')}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Articles to generate: {ARTICLES_PER_RUN}")
    if direction:
        print(f"Direction: {direction}")
    print(f"{'='*60}\n")

    results = {
        "started_at": start_time.isoformat(),
        "articles_attempted": 0,
        "articles_succeeded": 0,
        "errors": [],
        "articles": [],
    }

    # ─── Step 1: Topic Discovery ────────────────────────────────────────────────
    print("STEP 1: Topic Discovery")
    try:
        topics = discover_topics(n_topics=ARTICLES_PER_RUN + 2, direction=direction)  # Extra buffer for failures
        if not topics:
            raise RuntimeError("No topics discovered — check Brave Search API key and quota")
        print(f"  Discovered {len(topics)} topics:")
        for i, t in enumerate(topics, 1):
            print(f"    {i}. {t['title']}")
    except Exception as e:
        error_msg = f"Topic discovery failed: {e}"
        print(f"  ERROR: {error_msg}")
        results["errors"].append(error_msg)
        return results

    # ─── Steps 2–4: Generate Articles, LinkedIn, Images ─────────────────────────
    articles = []
    linkedin_posts_list = []
    image_results_list = []

    topics_to_process = topics[:ARTICLES_PER_RUN]

    for i, topic in enumerate(topics_to_process, 1):
        print(f"\nPROCESSING ARTICLE {i}/{len(topics_to_process)}: {topic['title']}")
        results["articles_attempted"] += 1

        # Step 2: Article Generation
        print("\nSTEP 2: Article Generation")
        try:
            article = generate_article(topic)
            save_article(article, OUTPUT_DIR / "articles")
            articles.append(article)
        except Exception as e:
            error_msg = f"Article generation failed for '{topic['title']}': {e}"
            print(f"  ERROR: {error_msg}")
            traceback.print_exc()
            results["errors"].append(error_msg)
            continue  # Skip to next topic

        # Step 3: LinkedIn Posts
        print("\nSTEP 3: LinkedIn Post Generation")
        try:
            variants = generate_linkedin_posts(article)
            save_linkedin_posts(article, variants, OUTPUT_DIR / "linkedin")
            linkedin_posts_list.append(variants)
        except Exception as e:
            error_msg = f"LinkedIn generation failed for '{article['title']}': {e}"
            print(f"  ERROR: {error_msg}")
            traceback.print_exc()
            results["errors"].append(error_msg)
            linkedin_posts_list.append({"hook": "", "insight": "", "story": ""})

        # Step 4: Image Generation
        print("\nSTEP 4: Image Generation")
        image_result = None
        try:
            image_result = generate_image(article)
            save_image(image_result, OUTPUT_DIR / "images")
        except Exception as e:
            error_msg = f"Image generation failed for '{article['title']}': {e}"
            print(f"  ERROR: {error_msg}")
            traceback.print_exc()
            results["errors"].append(error_msg)
            # Image failure is non-fatal — continue without it

        image_results_list.append(image_result)
        results["articles_succeeded"] += 1
        results["articles"].append({
            "slug": article["slug"],
            "title": article["title"],
            "validation_errors": article.get("validation_errors", []),
            "token_usage": article.get("token_usage", {}),
        })

    if not articles:
        print("\nNo articles generated — skipping email digest")
        return results

    # ─── Step 5: Email Digest ───────────────────────────────────────────────────
    print(f"\nSTEP 5: Email Digest")
    try:
        sent = send_digest(
            articles=articles,
            linkedin_posts=linkedin_posts_list,
            image_results=image_results_list,
            dry_run=dry_run,
        )
        if not sent:
            results["errors"].append("Email digest failed to send")
    except Exception as e:
        error_msg = f"Email digest error: {e}"
        print(f"  ERROR: {error_msg}")
        traceback.print_exc()
        results["errors"].append(error_msg)

    # ─── Step 6: Persist to known_papers.json ──────────────────────────────────
    if not dry_run:
        print("\nSTEP 6: Updating known_papers.json")
        for article in articles:
            add_paper_to_known(article["slug"], article["title"])

    # ─── Summary ────────────────────────────────────────────────────────────────
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    results["completed_at"] = end_time.isoformat()
    results["duration_seconds"] = duration

    print(f"\n{'='*60}")
    print(f"Pipeline Complete")
    print(f"Duration: {duration:.0f}s")
    print(f"Articles generated: {results['articles_succeeded']}/{results['articles_attempted']}")
    if results["errors"]:
        print(f"Errors ({len(results['errors'])}):")
        for err in results["errors"]:
            print(f"  - {err}")
    print(f"{'='*60}\n")

    return results


if __name__ == "__main__":
    # Allow --dry-run flag from CLI
    dry = DRY_RUN or "--dry-run" in sys.argv
    summary = run_pipeline(dry_run=dry)

    # Save run summary to output/
    summary_path = OUTPUT_DIR / "last_run.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        # Convert any non-serializable values
        json.dump(summary, f, indent=2, default=str)

    sys.exit(0 if not summary["errors"] else 1)
