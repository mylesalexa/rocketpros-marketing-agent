"""
RocketPros Marketing Agent — Web Dashboard
Runs alongside the scheduler on Railway PRO.

Routes:
  GET  /           — Dashboard: list of pending articles
  GET  /article/{slug}  — Article detail with LinkedIn posts + TypeScript preview
  POST /publish/{slug}  — Publish article to rprosite-main via GitHub API
  GET  /health     — Health check
"""

import os
import secrets
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse

from modules.publisher import publish_article, get_pending_articles, get_linkedin_posts

app = FastAPI(title="RocketPros Marketing Agent", docs_url=None, redoc_url=None)
security = HTTPBasic()

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "myles")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Enforce HTTP Basic Auth on all dashboard routes."""
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=500, detail="DASHBOARD_PASSWORD env var not set")
    user_ok = secrets.compare_digest(credentials.username.encode(), DASHBOARD_USER.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), DASHBOARD_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")
OUTPUT_DIR = Path(__file__).parent / "output"


# ── Shared CSS ─────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; min-height: 100vh; }
.wrap { max-width: 900px; margin: 0 auto; padding: 32px 16px; }
.header { border-bottom: 2px solid #06b6d4; padding-bottom: 20px; margin-bottom: 32px;
          display: flex; align-items: center; justify-content: space-between; }
.header h1 { color: #06b6d4; font-size: 20px; font-weight: 700; }
.header p { color: #64748b; font-size: 13px; margin-top: 4px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px;
         font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.badge-cyan { background: #06b6d4; color: #0f1117; }
.badge-violet { background: #8b5cf6; color: #fff; }
.badge-green { background: #16a34a; color: #fff; }
.badge-gray { background: #374151; color: #9ca3af; }
.card { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 10px;
        padding: 24px; margin-bottom: 20px; }
.card h2 { font-size: 17px; font-weight: 700; color: #f1f5f9; margin-bottom: 6px; }
.card .meta { color: #64748b; font-size: 13px; margin-bottom: 12px; }
.card .abstract { color: #94a3b8; font-size: 14px; line-height: 1.6; margin-bottom: 16px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.btn { display: inline-block; padding: 8px 18px; border-radius: 6px; font-size: 13px;
       font-weight: 600; cursor: pointer; border: none; text-decoration: none;
       transition: opacity .15s; }
.btn:hover { opacity: .85; }
.btn-primary { background: #06b6d4; color: #0f1117; }
.btn-secondary { background: #1e293b; color: #94a3b8; border: 1px solid #374151; }
.btn-publish { background: #16a34a; color: #fff; font-size: 14px; padding: 10px 24px; }
.btn-danger { background: #7f1d1d; color: #fca5a5; }
.empty { text-align: center; padding: 60px 0; color: #475569; }
.empty h2 { font-size: 18px; margin-bottom: 8px; color: #64748b; }
pre { background: #0a0d16; border: 1px solid #2d3748; border-radius: 8px; padding: 16px;
      font-size: 11px; color: #a5f3fc; overflow-x: auto; white-space: pre-wrap;
      max-height: 400px; overflow-y: auto; }
.linkedin-block { background: #0f1117; border: 1px solid #2d3748; border-radius: 8px;
                  padding: 16px; margin-bottom: 12px; font-size: 13px; color: #cbd5e1;
                  line-height: 1.7; white-space: pre-wrap; }
.section-label { font-size: 11px; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .1em; color: #8b5cf6; margin: 24px 0 10px 0;
                 padding-top: 20px; border-top: 1px solid #2d3748; }
.success-banner { background: #052e16; border: 1px solid #16a34a; border-radius: 8px;
                  padding: 16px; margin-bottom: 20px; color: #4ade80; font-size: 14px; }
.error-banner { background: #450a0a; border: 1px solid #dc2626; border-radius: 8px;
                padding: 16px; margin-bottom: 20px; color: #fca5a5; font-size: 14px; }
.back { color: #06b6d4; text-decoration: none; font-size: 13px; }
.back:hover { text-decoration: underline; }
.icons { display: flex; gap: 8px; margin-bottom: 8px; }
"""


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(require_auth)):
    articles = get_pending_articles()

    if not articles:
        body = """
        <div class="empty">
          <h2>No articles yet</h2>
          <p>Articles will appear here after the daily pipeline runs.</p>
        </div>"""
    else:
        cards = ""
        for a in articles:
            img_badge = '<span class="badge badge-cyan">Image ✓</span>' if a["has_image"] else '<span class="badge badge-gray">No image</span>'
            li_badge = '<span class="badge badge-violet">LinkedIn ✓</span>' if a["has_linkedin"] else '<span class="badge badge-gray">No LinkedIn</span>'
            cards += f"""
            <div class="card">
              <div class="icons">{img_badge} {li_badge}</div>
              <h2>{a['title']}</h2>
              <div class="meta">{a['slug']} &middot; {a['read_time']} &middot; Generated {a['modified']}</div>
              <div class="abstract">{a['abstract']}</div>
              <div class="actions">
                <a href="/article/{a['slug']}" class="btn btn-secondary">Preview &rarr;</a>
                <button onclick="publishArticle('{a['slug']}', this)" class="btn btn-publish">
                  🚀 Publish to Website
                </button>
              </div>
              <div id="result-{a['slug']}" style="margin-top:12px;"></div>
            </div>"""
        body = cards

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RocketPros — Article Dashboard</title>
  <style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <h1>🚀 RocketPros Article Dashboard</h1>
      <p>{len(articles)} article(s) pending review</p>
    </div>
    <span class="badge badge-cyan">Marketing Agent</span>
  </div>
  {body}
</div>
<script>
async function publishArticle(slug, btn) {{
  btn.disabled = true;
  btn.textContent = 'Publishing...';
  const resultDiv = document.getElementById('result-' + slug);
  try {{
    const resp = await fetch('/publish/' + slug, {{ method: 'POST' }});
    const data = await resp.json();
    if (data.success) {{
      resultDiv.innerHTML = '<div class="success-banner">✅ ' + data.message +
        (data.url ? ' <a href="' + data.url + '" target="_blank" style="color:#4ade80">' + data.url + '</a>' : '') + '</div>';
      btn.textContent = '✅ Published';
      btn.style.background = '#374151';
    }} else {{
      resultDiv.innerHTML = '<div class="error-banner">❌ ' + data.message + '</div>';
      btn.disabled = false;
      btn.textContent = '🚀 Publish to Website';
    }}
  }} catch(e) {{
    resultDiv.innerHTML = '<div class="error-banner">❌ Network error: ' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = '🚀 Publish to Website';
  }}
}}
</script>
</body>
</html>"""
    return html


@app.get("/article/{slug}", response_class=HTMLResponse)
def article_detail(slug: str, _: None = Depends(require_auth)):
    articles = get_pending_articles()
    article = next((a for a in articles if a["slug"] == slug), None)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # Read raw TypeScript
    ts_path = OUTPUT_DIR / "articles" / f"{slug}.ts"
    ts_code = ts_path.read_text(encoding="utf-8") if ts_path.exists() else "(file not found)"

    # LinkedIn posts
    linkedin = get_linkedin_posts(slug)
    linkedin_html = ""
    for variant, label in [("hook", "Hook Post (Myles)"), ("insight", "Insight Post (Ali)"), ("story", "Story Post (Myles)")]:
        post = linkedin.get(variant, "")
        if post:
            linkedin_html += f'<div style="font-size:12px;color:#64748b;margin-bottom:4px;">{label}</div>'
            linkedin_html += f'<div class="linkedin-block">{post}</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{article['title']}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <a href="/" class="back">&larr; Back to dashboard</a>
      <h1 style="margin-top:8px">{article['title']}</h1>
      <p>{slug} &middot; {article['read_time']} &middot; Generated {article['modified']}</p>
    </div>
  </div>

  <div id="result" style="margin-bottom:16px;"></div>

  <div class="actions" style="margin-bottom:32px;">
    <button onclick="publishArticle('{slug}', this)" class="btn btn-publish">
      🚀 Publish to Website
    </button>
    <a href="{SITE_URL}/research/{slug}" target="_blank" class="btn btn-secondary">
      Preview Live URL &rarr;
    </a>
  </div>

  {'<div class="section-label">LinkedIn Posts</div>' + linkedin_html if linkedin_html else ''}

  <div class="section-label">TypeScript — /lib/research/papers/{slug}.ts</div>
  <pre>{ts_code}</pre>

</div>
<script>
async function publishArticle(slug, btn) {{
  btn.disabled = true;
  btn.textContent = 'Publishing...';
  const resultDiv = document.getElementById('result');
  try {{
    const resp = await fetch('/publish/' + slug, {{ method: 'POST' }});
    const data = await resp.json();
    if (data.success) {{
      resultDiv.innerHTML = '<div class="success-banner">✅ ' + data.message +
        (data.url ? ' &rarr; <a href="' + data.url + '" target="_blank" style="color:#4ade80">' + data.url + '</a>' : '') + '</div>';
      btn.textContent = '✅ Published';
      btn.style.background = '#374151';
    }} else {{
      resultDiv.innerHTML = '<div class="error-banner">❌ ' + data.message + '</div>';
      btn.disabled = false;
      btn.textContent = '🚀 Publish to Website';
    }}
  }} catch(e) {{
    resultDiv.innerHTML = '<div class="error-banner">❌ Network error: ' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = '🚀 Publish to Website';
  }}
}}
</script>
</body>
</html>"""
    return html


@app.post("/publish/{slug}")
def publish(slug: str, _: None = Depends(require_auth)):
    result = publish_article(slug)
    return JSONResponse(content=result)
