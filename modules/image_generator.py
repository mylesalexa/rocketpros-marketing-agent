"""
Image generator — produces branded hero images via OpenAI gpt-image-1.

Upgrade from DALL-E 3: gpt-image-1 (released April 2025) produces dramatically
better compositional accuracy, follows complex prompts faithfully, and handles
professional/technical scenes without the cartoonish drift of DALL-E 3.

Output: 1536x1024 landscape PNG (matches OpenGraph + article hero dimensions).
"""

import os
import base64
import httpx
from pathlib import Path

from openai import OpenAI


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Brand style block — injected into every prompt.
# gpt-image-1 follows style instructions far more precisely than DALL-E 3.
BRAND_STYLE = """
Style: Ultra-sharp professional photography, cinematic lighting.
Color grading: deep navy-black background (#0f1117), accent lighting in electric cyan (#06b6d4)
and violet (#8b5cf6). Think high-end automotive + technology brand campaign.
Mood: authoritative, modern, precise.
NO text, logos, watermarks, or UI overlays in the image.
Aspect ratio: wide cinematic landscape (3:2). Shot on Phase One medium format.
""".strip()

# Rich scene library — gpt-image-1 handles these detailed descriptions accurately
SCENES: dict[str, str] = {
    "adas": (
        "A sleek modern collision repair bay photographed at night. "
        "An advanced vehicle sits on a precision alignment rack surrounded by ADAS calibration targets "
        "— tall white reflective panels with circular targets, precisely positioned in front of the "
        "vehicle's cameras and radar sensors. Electric cyan laser lines project across the shop floor. "
        "The scene has the aesthetic of a high-tech laboratory meeting an automotive workshop."
    ),
    "calibration": (
        "Close-up cinematic shot: ADAS calibration targets positioned in front of a modern vehicle's "
        "front sensors. The targets are crisp white with geometric patterns, lit by directional studio "
        "lighting that creates sharp shadows. A technician's hands are barely visible making micro-adjustments "
        "to calibration equipment. Bokeh background shows a professional shop environment."
    ),
    "scan": (
        "A collision repair technician performing a pre-repair diagnostic scan on a modern vehicle. "
        "The technician holds a professional OBD scanner, while a large diagnostic screen behind them "
        "glows with vehicle system data — cyan and violet data readouts showing fault codes, sensor "
        "status, and vehicle network health. The shop lighting creates dramatic rim lighting on both "
        "the technician and the vehicle."
    ),
    "documentation": (
        "An estimator's workstation in a collision repair office — two large monitors displaying "
        "professional estimation software (Mitchell) with repair line items. A physical repair photo "
        "is displayed alongside digital documentation. The desk has a tablet showing the vehicle "
        "inspection report. Overhead lighting in cyan creates a focused, high-tech workspace feel. "
        "Clean, organized, professional."
    ),
    "supplement": (
        "A collision repair estimator reviewing a supplement request on a high-resolution monitor. "
        "The screen shows a detailed repair estimate with line items highlighted. In the background, "
        "through a large shop window, you can see the vehicle being repaired. The professional is "
        "focused, the data is precise. Cyan monitor glow illuminates the scene dramatically."
    ),
    "cycle time": (
        "Wide-angle shot of a busy collision repair production floor shot from above at a slight angle. "
        "Multiple vehicles in various stages of repair — some on lifts, some at prep stations. "
        "A large production board on the wall shows vehicle status indicators in cyan and amber. "
        "The shop is immaculate and organized. Overhead industrial lighting creates dramatic parallel "
        "light beams across the workspace."
    ),
    "mpi": (
        "A premium collision repair shop interior in Manitoba — modern, clean, and professionally lit. "
        "A late-model vehicle sits at a repair station under professional shop lighting. The shop has "
        "the aesthetic of a dealership body shop — organized, high-tech, accredited. "
        "Cyan accent lighting runs along the ceiling. Shot during golden hour through large bay doors."
    ),
    "sgi": (
        "A Saskatchewan collision repair facility — wide shot showing modern frame straightening "
        "equipment and a vehicle under repair. The frame machine is a professional Car-O-Liner unit. "
        "Industrial lighting with cyan LED accents creates the high-tech professional aesthetic. "
        "The shop is clean, certified, and equipment-heavy."
    ),
    "aluminum": (
        "Close-up of specialized aluminum welding in a collision repair shop. A technician in "
        "full protective gear works on an aluminum vehicle panel. The welding arc produces brilliant "
        "electric-cyan light that illuminates the scene dramatically. Surrounding the workstation are "
        "specialized aluminum tools. High-contrast, technically precise imagery."
    ),
    "parts": (
        "A modern OEM parts storage area in a collision repair facility. Floor-to-ceiling shelving "
        "holds organized, labeled OEM parts in clear packaging. A large inventory management screen "
        "on the wall shows real-time parts status with cyan data readouts. The space is immaculate — "
        "the standard of a high-volume, carrier-approved facility."
    ),
    "oem": (
        "A collision repair engineer reviewing OEM repair procedure documentation on a large wall-mounted "
        "screen. The screen shows detailed vehicle repair procedures with technical diagrams. "
        "In the background, the actual vehicle being repaired is visible. The scene conveys precision, "
        "compliance, and technical expertise. Violet and cyan accent lighting."
    ),
    "severity": (
        "A collision repair analytics dashboard displayed across multiple large monitors in a modern "
        "operations office. The screens show data visualizations — trend lines, bar charts, heat maps — "
        "all in the brand palette of cyan, violet, and white on dark navy backgrounds. "
        "The data tells a story of performance metrics and financial trends. Shot from a low angle "
        "to make the monitors look imposing and important."
    ),
    "ev": (
        "An electric vehicle on a specialized EV lift in a modern collision repair facility. "
        "High-voltage warning labels are visible on the battery access panels. The shop has dedicated "
        "EV charging infrastructure visible in the background. A technician in specialized insulated "
        "gear reviews the vehicle's battery and structural damage. Cyan accent lighting creates "
        "a futuristic, high-tech atmosphere."
    ),
    "rental": (
        "A collision repair front desk — the moment of vehicle handoff. A customer receives their keys "
        "while a rental car is visible through the window in the background. The desk is modern and "
        "professional, with a digital check-in display. The scene communicates efficiency, "
        "customer service, and program compliance. Warm lighting with cyan brand accents."
    ),
    "labour": (
        "An experienced collision repair estimator photographed at their workstation — dual monitors "
        "running Mitchell Estimating software showing a detailed repair estimate with labor operations "
        "and parts lines. The estimator is mid-focus, working with precision. The screen glow "
        "illuminates their face in cyan. In the background, the shop floor is visible through glass."
    ),
    "insurance": (
        "A split-scene composition: left side shows a collision repair shop with a vehicle being "
        "repaired, right side shows a professional reviewing claim data on a tablet. "
        "The two halves are connected by data visualization elements — lines, charts, approval "
        "checkmarks — that float between the repair world and the insurance world. Cyan connects them."
    ),
    "data": (
        "Abstract but grounded data visualization — a dark navy background with flowing streams of "
        "vehicle repair data: VIN numbers, dollar amounts, day counts, approval rates. "
        "The data forms the silhouette of a vehicle. Cyan and violet gradients. "
        "Photorealistic rendering, not cartoon or illustrative. The aesthetic of a Bloomberg terminal "
        "meets a collision repair analytics platform."
    ),
    "rps": (
        "A collision repair shop manager reviewing their RPS performance scorecard on a large monitor. "
        "The screen shows a performance dashboard with scores across categories — estimate accuracy, "
        "cycle time, customer experience. The scores are displayed as clean data visualizations in "
        "cyan and violet. The manager's focused expression conveys the importance of this data."
    ),
    "drp": (
        "A collision repair shop with multiple carrier-branded program certificates visible on a "
        "professional wall display. The shop is modern, organized, and clearly accredited. "
        "A vehicle is visible in the background at a repair station. "
        "The scene communicates carrier trust, accreditation, and professionalism."
    ),
}

# Default fallback scene
_DEFAULT_SCENE = (
    "A wide establishing shot of a modern, high-tech collision repair facility. "
    "Multiple bays are visible — some with vehicles on lifts, others at finishing stations. "
    "The lighting is dramatic: overhead industrial fixtures with cyan LED accent strips along "
    "the walls and ceiling. The shop is immaculate, organized, and unmistakably professional. "
    "Shot with a wide-angle lens from the entrance, creating depth and scale."
)


def _build_prompt(article_title: str, article_slug: str) -> str:
    """
    Build a gpt-image-1 prompt by matching article topic to a richly detailed scene.
    gpt-image-1 handles complex, multi-sentence prompts with high fidelity.
    """
    combined = (article_slug + " " + article_title).lower()

    scene = _DEFAULT_SCENE
    for keyword, scene_desc in SCENES.items():
        if keyword in combined:
            scene = scene_desc
            break

    return f"{scene}\n\n{BRAND_STYLE}"


def generate_image(article: dict) -> dict:
    """
    Generate a hero image for the article using OpenAI gpt-image-1.

    gpt-image-1 (April 2025) vs DALL-E 3:
    - Dramatically better compositional accuracy on complex prompts
    - Correct handling of professional/technical scenes
    - No cartoonish drift or incorrect object placement
    - Returns base64 directly (no temp URL to download)

    Args:
        article: dict with keys: title, slug

    Returns:
        dict with keys: image_bytes (bytes), prompt (str), slug (str)
    """
    client = OpenAI(api_key=OPENAI_API_KEY)

    title = article.get("title", "")
    slug = article.get("slug", "")
    prompt = _build_prompt(title, slug)

    print(f"  [image_generator] Generating image (gpt-image-1) for: '{title}'")
    print(f"  [image_generator] Scene: {prompt[:120]}...")

    response = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1536x1024",   # landscape hero — wider than 1792x1024, sharper output
        quality="high",     # options: low / medium / high / auto
        n=1,
    )

    # gpt-image-1 returns base64-encoded image data (no temporary URL)
    image_b64 = response.data[0].b64_json
    image_bytes = base64.b64decode(image_b64)

    print(f"  [image_generator] Done. Size: {len(image_bytes) / 1024:.0f} KB")

    return {
        "image_bytes": image_bytes,
        "prompt": prompt,
        "slug": slug,
    }


def generate_image_from_prompt(custom_prompt: str, slug: str) -> dict:
    """
    Generate an image from a fully custom prompt (used by the dashboard regen UI).
    """
    client = OpenAI(api_key=OPENAI_API_KEY)
    full_prompt = f"{custom_prompt}\n\n{BRAND_STYLE}"

    print(f"  [image_generator] Generating from custom prompt for: {slug}")

    response = client.images.generate(
        model="gpt-image-1",
        prompt=full_prompt,
        size="1536x1024",
        quality="high",
        n=1,
    )

    image_bytes = base64.b64decode(response.data[0].b64_json)
    return {"image_bytes": image_bytes, "prompt": full_prompt, "slug": slug}


def save_image(image_result: dict, output_dir: Path) -> Path:
    """Save image bytes to output/images/<slug>.png"""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = image_result["slug"]
    file_path = output_dir / f"{slug}.png"
    file_path.write_bytes(image_result["image_bytes"])
    print(f"  [image_generator] Saved: {file_path}")
    return file_path
