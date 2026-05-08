"""
Email digest module — assembles and sends the daily article digest via Resend.
Includes article previews, full TypeScript code blocks, LinkedIn variants,
and hero image attachments.
"""

import os
import base64
import json
from datetime import datetime
from pathlib import Path
from html import escape

import resend


RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "agent@rocketpros.app")
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")

EMAIL_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "email_template.html"


def _load_template() -> str:
    with open(EMAIL_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _format_article_block(article: dict, linkedin_variants: dict, image_result: dict | None, index: int) -> str:
    """
    Render one article's full HTML block for the email digest.
    """
    title = escape(article.get("title", "Untitled"))
    slug = article.get("slug", "")
    ts_code = article.get("ts_code", "")
    validation_errors = article.get("validation_errors", [])
    quality_flags = article.get("quality_flags", [])
    citation_summary = article.get("citation_summary", {})
    topic = article.get("topic", {})
    audience = escape(topic.get("audience", "Collision Repair Shops + Insurers"))

    # Extract abstract and key findings from TypeScript (reuse helpers)
    from modules.linkedin_generator import _extract_field, _extract_array_field
    abstract = escape(_extract_field(ts_code, "abstract") or "")
    key_findings = _extract_array_field(ts_code, "keyFindings")
    read_time = _extract_field(ts_code, "readTime") or "13 min"
    category = escape(_extract_field(ts_code, "category") or "Research")

    article_url = f"{SITE_URL}/research/{slug}"

    html = f'<div class="article-block">\n'
    html += f'  <div class="article-number">Article {index}</div>\n'
    html += f'  <h2 class="article-title">{title}</h2>\n'
    html += f'  <div class="article-meta">'
    html += f'<span>📂 {category}</span>'
    html += f'<span>👥 {audience}</span>'
    html += f'<span>⏱ {escape(read_time)} read</span>'
    html += f'</div>\n'

    # Schema validation warnings
    if validation_errors:
        html += '  <div class="validation-warning">\n'
        html += '    ⚠️ Schema validation warnings (review before publishing):<br>\n'
        for err in validation_errors:
            html += f'    &bull; {escape(err)}<br>\n'
        html += '  </div>\n'

    # Quality flags (anti-slop, citation quality, currentness)
    if quality_flags:
        html += '  <div class="validation-warning">\n'
        html += '    🔍 Content quality flags (review before publishing):<br>\n'
        for flag in quality_flags:
            html += f'    &bull; {escape(flag)}<br>\n'
        html += '  </div>\n'

    # Citation verification summary
    if citation_summary:
        dead = citation_summary.get("dead", 0)
        untrusted = citation_summary.get("untrusted", 0)
        ok = citation_summary.get("ok", 0)
        checked = citation_summary.get("checked", 0)
        if dead > 0 or untrusted > 0:
            html += '  <div class="validation-warning">\n'
            html += f'    🔗 Citation check: {ok}/{checked} OK'
            if dead:
                html += f', {dead} dead link(s)'
            if untrusted:
                html += f', {untrusted} from non-approved domain(s)'
            html += '<br>\n'
            for flag in citation_summary.get("quality_flags", []):
                html += f'    &bull; {escape(flag)}<br>\n'
            html += '  </div>\n'
        else:
            html += f'  <div class="citation-ok">✅ Citations: {ok}/{checked} verified reachable</div>\n'

    # Hero image (inline base64)
    if image_result and image_result.get("image_bytes"):
        img_b64 = base64.b64encode(image_result["image_bytes"]).decode()
        html += f'  <img class="hero-image" src="data:image/png;base64,{img_b64}" alt="{title} hero image" />\n'

    # Abstract
    if abstract:
        html += f'  <div class="abstract">{abstract}</div>\n'

    # Key findings
    if key_findings:
        html += '  <div class="key-findings">\n'
        html += '    <h3>Key Findings</h3>\n'
        html += '    <ul>\n'
        for finding in key_findings[:5]:
            html += f'      <li>{escape(finding)}</li>\n'
        html += '    </ul>\n'
        html += '  </div>\n'

    # LinkedIn posts
    html += '  <div class="section-label">LinkedIn Posts (Copy-Paste Ready)</div>\n'
    for variant_key, variant_label in [("hook", "Hook Post"), ("insight", "Insight Post"), ("story", "Story Post")]:
        post_text = linkedin_variants.get(variant_key, "")
        if post_text:
            html += f'  <div class="linkedin-variant-label">{variant_label}</div>\n'
            html += f'  <div class="linkedin-post">{escape(post_text)}</div>\n'

    # TypeScript code block
    html += '  <div class="section-label">TypeScript — Copy to /lib/research/papers/' + escape(slug) + '.ts</div>\n'
    html += f'  <div class="code-block">{escape(ts_code)}</div>\n'

    # Publish instructions
    html += '''  <div class="publish-steps">
    <h4>To Publish This Article</h4>
    <ol>
      <li>Copy TypeScript above → <code>/lib/research/papers/''' + escape(slug) + '''.ts</code></li>
      <li>Copy attached image → <code>/public/images/''' + escape(slug) + '''.png</code></li>
      <li>Add to <code>/lib/research/index.ts</code>: import + push to papers[] array</li>
      <li><code>git push</code> → Vercel auto-deploys in ~60 seconds</li>
      <li>Verify live at: <a href="''' + article_url + '''" style="color:#06b6d4">''' + article_url + '''</a></li>
    </ol>
  </div>\n'''

    html += '</div>\n'
    return html


def send_digest(
    articles: list[dict],
    linkedin_posts: list[dict],
    image_results: list[dict | None],
    dry_run: bool = False,
) -> bool:
    """
    Assemble and send the daily digest email via Resend.

    Args:
        articles: list of article dicts from article_generator
        linkedin_posts: list of variant dicts from linkedin_generator (same order as articles)
        image_results: list of image result dicts (or None if image gen failed)
        dry_run: if True, print the email instead of sending

    Returns:
        True if sent successfully (or dry_run), False if send failed
    """
    if not RESEND_API_KEY and not dry_run:
        print("[email_digest] ERROR: RESEND_API_KEY not set")
        return False
    if not ADMIN_EMAIL and not dry_run:
        print("[email_digest] ERROR: ADMIN_EMAIL not set")
        return False

    today = datetime.now().strftime("%B %d, %Y")
    article_count = len(articles)
    subject = f"🚀 RocketPros Daily Articles — {today} ({article_count} article{'s' if article_count != 1 else ''} ready)"

    template = _load_template()

    # Build article blocks
    article_blocks_html = ""
    attachments = []

    for i, (article, variants, image_result) in enumerate(zip(articles, linkedin_posts, image_results), start=1):
        article_blocks_html += _format_article_block(article, variants, image_result, i)

        # Add image as attachment (for saving to /public/images/)
        if image_result and image_result.get("image_bytes"):
            slug = article.get("slug", f"article-{i}")
            attachments.append({
                "filename": f"{slug}.png",
                "content": base64.b64encode(image_result["image_bytes"]).decode(),
            })

    html_body = template.replace("{{date}}", today)
    html_body = html_body.replace("{{article_count}}", str(article_count))
    html_body = html_body.replace("{{article_blocks}}", article_blocks_html)

    if dry_run:
        print(f"\n[email_digest] DRY RUN — Would send to: {ADMIN_EMAIL}")
        print(f"[email_digest] Subject: {subject}")
        print(f"[email_digest] Articles: {[a['title'] for a in articles]}")
        print(f"[email_digest] Attachments: {[a['filename'] for a in attachments]}")
        print("[email_digest] HTML length:", len(html_body), "chars")
        return True

    # Send via Resend
    resend.api_key = RESEND_API_KEY
    print(f"[email_digest] Sending digest to {ADMIN_EMAIL}...")

    try:
        params = {
            "from": EMAIL_FROM,
            "to": [ADMIN_EMAIL],
            "subject": subject,
            "html": html_body,
        }
        if attachments:
            params["attachments"] = attachments

        result = resend.Emails.send(params)
        print(f"[email_digest] Sent! ID: {result.get('id', 'unknown')}")
        return True

    except Exception as e:
        print(f"[email_digest] ERROR sending email: {e}")
        return False
