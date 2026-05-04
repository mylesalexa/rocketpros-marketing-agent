"""
Image generator — produces branded hero images via OpenAI DALL-E 3.
Dark theme, cyan/violet palette, 1792x1024 (matches OpenGraph + article hero).
"""

import os
import re
import httpx
from pathlib import Path

from openai import OpenAI


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Base DALL-E 3 style instruction — constant across all images
STYLE_PREFIX = (
    "Dark-themed professional infographic for a collision repair technology company. "
    "Color palette: deep navy (#0f1117) background, cyan (#06b6d4) and violet (#8b5cf6) accents. "
    "Photorealistic, wide-format (1792x1024), clean and technical aesthetic. "
    "No text overlay. Corporate SaaS visual style. "
)

# Topic-to-scene mapping for better DALL-E prompts
SCENE_KEYWORDS = {
    "adas": "a modern body shop with ADAS diagnostic equipment scanning a vehicle under cyan LED lighting, with calibration targets visible",
    "calibration": "precise ADAS calibration targets positioned in front of a damaged vehicle in a professional collision repair bay",
    "scan": "a technician holding a handheld scanning device connected to a modern vehicle in a dark-lit collision repair shop",
    "documentation": "a digital tablet displaying repair documentation software with vehicle damage photos on a modern shop desk",
    "supplement": "a collision estimator reviewing a supplement on a large monitor showing repair line items and insurer approval",
    "cycle time": "a production board in a busy collision repair shop showing vehicle statuses and target completion dates",
    "mpi": "a Manitoba collision repair shop interior with modern equipment and MPI signage, professional lighting",
    "sgi": "a Saskatchewan collision repair facility with modern frame straightening equipment under industrial lighting",
    "aluminum": "specialized aluminum welding equipment and repair tools in a modern collision repair shop",
    "parts": "organized OEM parts on shelving in a modern collision repair facility with inventory management screens",
    "oem": "OEM position statement documents on a screen alongside a modern vehicle undergoing structural repair",
    "severity": "data visualization dashboard showing collision repair cost trends on large monitors in a modern shop office",
    "ev": "an electric vehicle on a repair lift in a modern collision shop with high-voltage warning signs",
    "rental": "a collision shop front desk with a vehicle handoff in progress, showing rental car logistics",
    "labour": "a skilled collision repair estimator at a workstation reviewing hour estimates on Mitchell software",
}


def _build_dalle_prompt(article_title: str, article_slug: str) -> str:
    """
    Build a DALL-E 3 prompt by matching the article topic to a scene description.
    Falls back to a generic collision repair scene if no keyword matches.
    """
    slug_lower = article_slug.lower()
    title_lower = article_title.lower()
    combined = slug_lower + " " + title_lower

    # Find the best matching scene
    scene = None
    for keyword, scene_desc in SCENE_KEYWORDS.items():
        if keyword in combined:
            scene = scene_desc
            break

    if not scene:
        scene = (
            "a modern, high-tech collision repair shop interior with a damaged vehicle "
            "on a lift, diagnostic equipment, and a technician reviewing data on a screen"
        )

    return STYLE_PREFIX + "Subject: " + scene + "."


def generate_image(article: dict) -> dict:
    """
    Generate a hero image for the article using DALL-E 3.

    Args:
        article: dict with keys: title, slug

    Returns:
        dict with keys:
            - image_url: str (temporary OpenAI URL — download immediately)
            - image_bytes: bytes
            - prompt: str (the DALL-E prompt used)
            - slug: str
    """
    client = OpenAI(api_key=OPENAI_API_KEY)

    title = article.get("title", "")
    slug = article.get("slug", "")
    prompt = _build_dalle_prompt(title, slug)

    print(f"  [image_generator] Generating image for: '{title}'")
    print(f"  [image_generator] Prompt: {prompt[:120]}...")

    response = client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size="1792x1024",
        quality="hd",
        n=1,
    )

    image_url = response.data[0].url
    revised_prompt = response.data[0].revised_prompt or prompt

    # Download the image immediately (OpenAI URLs expire in ~1 hour)
    print(f"  [image_generator] Downloading image...")
    with httpx.Client(timeout=30.0) as http_client:
        img_response = http_client.get(image_url)
        img_response.raise_for_status()
        image_bytes = img_response.content

    print(f"  [image_generator] Done. Image size: {len(image_bytes) / 1024:.0f} KB")

    return {
        "image_url": image_url,
        "image_bytes": image_bytes,
        "prompt": revised_prompt,
        "slug": slug,
    }


def save_image(image_result: dict, output_dir: Path) -> Path:
    """Save image bytes to output/images/<slug>.png"""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = image_result["slug"]
    file_path = output_dir / f"{slug}.png"
    file_path.write_bytes(image_result["image_bytes"])
    print(f"  [image_generator] Saved: {file_path}")
    return file_path
