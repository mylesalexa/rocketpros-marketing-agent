"""
RocketPros Marketing Agent — Web Dashboard
Runs alongside the scheduler on Railway PRO.

Routes:
  GET  /                  — Dashboard: articles + force-run console
  GET  /article/{slug}    — Article detail with LinkedIn posts + TypeScript preview
  POST /publish/{slug}    — Publish article to rprosite-main via GitHub API
  POST /run               — Force-trigger the pipeline immediately
  GET  /run/stream        — SSE stream of live pipeline logs
  GET  /run/status        — JSON: { running, log_count }
  GET  /health            — Health check (no auth)
"""

import os
import sys
import secrets
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from modules.publisher import publish_article, get_pending_articles, get_linkedin_posts, delete_article

app = FastAPI(title="RocketPros Marketing Agent", docs_url=None, redoc_url=None)
security = HTTPBasic()

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "myles")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/output"))


# ── Run state — shared between pipeline thread and SSE stream ──────────────────

class RunState:
    def __init__(self):
        self.running = False
        self.logs: list[str] = []
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self.running = True
            self.logs = []

    def append(self, line: str):
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S")
            self.logs.append(f"[{ts}] {line}")

    def finish(self):
        with self._lock:
            self.running = False

    def snapshot(self, from_idx: int) -> tuple[list[str], bool]:
        with self._lock:
            return self.logs[from_idx:], self.running


run_state = RunState()


# ── Run request body ───────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    direction: str = ""


# ── Stdout capture — pipes print() calls into run_state ───────────────────────

class LogCapture:
    """Replaces sys.stdout in the pipeline thread, feeding lines to RunState."""
    def __init__(self, state: RunState, original):
        self.state = state
        self.original = original
        self._buf = ""

    def write(self, text: str):
        self.original.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.state.append(line)

    def flush(self):
        self.original.flush()

    def fileno(self):
        return self.original.fileno()


# ── Auth ───────────────────────────────────────────────────────────────────────

def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
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


# ── Shared CSS ─────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; min-height: 100vh; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 32px 16px; }
.header { border-bottom: 2px solid #06b6d4; padding-bottom: 20px; margin-bottom: 32px;
          display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.header h1 { color: #06b6d4; font-size: 20px; font-weight: 700; }
.header p { color: #64748b; font-size: 13px; margin-top: 4px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px;
         font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.badge-cyan { background: #06b6d4; color: #0f1117; }
.badge-violet { background: #8b5cf6; color: #fff; }
.badge-green { background: #16a34a; color: #fff; }
.badge-gray { background: #374151; color: #9ca3af; }
.badge-orange { background: #c2410c; color: #fff; }
.layout { display: grid; grid-template-columns: 1fr 400px; gap: 24px; align-items: start; }
@media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }
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
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn-primary { background: #06b6d4; color: #0f1117; }
.btn-secondary { background: #1e293b; color: #94a3b8; border: 1px solid #374151; }
.btn-publish { background: #16a34a; color: #fff; font-size: 14px; padding: 10px 24px; }
.btn-run { background: #7c3aed; color: #fff; font-size: 14px; padding: 10px 24px; }
.btn-run.running { background: #374151; }
.btn-delete { background: #1e293b; color: #fca5a5; border: 1px solid #7f1d1d; }
.btn-delete:hover { background: #7f1d1d; color: #fff; }
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

/* Console panel */
.console-panel { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 10px;
                 padding: 0; overflow: hidden; position: sticky; top: 24px; }
.console-header { background: #0f1117; padding: 14px 18px;
                  border-bottom: 1px solid #2d3748; display: flex;
                  align-items: center; justify-content: space-between; }
.console-header h3 { font-size: 13px; font-weight: 700; color: #94a3b8;
                     text-transform: uppercase; letter-spacing: .08em; }
.console-status { font-size: 11px; font-weight: 700; padding: 2px 8px;
                  border-radius: 20px; text-transform: uppercase; letter-spacing: .06em; }
.console-status.idle { background: #1e293b; color: #475569; }
.console-status.running { background: #7c3aed22; color: #a78bfa;
                           animation: pulse 1.5s ease-in-out infinite; }
.console-status.done { background: #052e16; color: #4ade80; }
.console-status.error { background: #450a0a; color: #fca5a5; }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
.console-body { font-family: 'Courier New', monospace; font-size: 11px; line-height: 1.6;
                color: #94a3b8; padding: 14px 18px; height: 420px; overflow-y: auto;
                background: #0a0d16; }
.console-body .log-line { margin-bottom: 2px; }
.console-body .log-ts { color: #374151; margin-right: 6px; }
.console-body .log-text { color: #a5f3fc; }
.console-body .log-text.err { color: #fca5a5; }
.console-body .log-text.done { color: #4ade80; font-weight: 700; }
.console-body .log-text.step { color: #f59e0b; font-weight: 700; }
.console-empty { color: #374151; font-style: italic; }
.console-footer { padding: 14px 18px; border-top: 1px solid #2d3748; }
.direction-wrap { margin-bottom: 12px; }
.direction-label { font-size: 11px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: .08em; color: #64748b; margin-bottom: 6px; display: block; }
.direction-hint { font-size: 11px; color: #374151; margin-top: 4px; }
textarea.direction-input { width: 100%; background: #0a0d16; border: 1px solid #374151;
  border-radius: 6px; color: #e2e8f0; font-size: 12px; padding: 10px 12px;
  resize: vertical; min-height: 72px; font-family: inherit; line-height: 1.5;
  transition: border-color .15s; }
textarea.direction-input:focus { outline: none; border-color: #7c3aed; }
textarea.direction-input::placeholder { color: #374151; }
"""


# ── Pipeline runner ────────────────────────────────────────────────────────────

def _run_pipeline_thread(direction: str = ""):
    """Runs in a background thread. Captures all stdout into run_state."""
    original_stdout = sys.stdout
    capture = LogCapture(run_state, original_stdout)
    sys.stdout = capture

    try:
        mode = f"Directed: '{direction}'" if direction else "Autonomous"
        run_state.append(f"=== Pipeline started — {mode} ===")
        from pipeline import run_pipeline
        summary = run_pipeline(dry_run=DRY_RUN, direction=direction)

        errors = summary.get("errors", [])
        succeeded = summary.get("articles_succeeded", 0)
        duration = summary.get("duration_seconds", 0)

        run_state.append(f"=== Pipeline complete: {succeeded} article(s), {len(errors)} error(s), {duration:.0f}s ===")
    except Exception as e:
        run_state.append(f"ERROR: Pipeline crashed — {e}")
    finally:
        sys.stdout = original_stdout
        run_state.finish()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "running": run_state.running}


@app.get("/run/status")
def run_status(_: None = Depends(require_auth)):
    return JSONResponse({"running": run_state.running, "log_count": len(run_state.logs)})


@app.post("/run")
def force_run(body: RunRequest = RunRequest(), _: None = Depends(require_auth)):
    if run_state.running:
        return JSONResponse({"started": False, "message": "Pipeline is already running"})
    run_state.start()
    direction = body.direction.strip()
    t = threading.Thread(target=_run_pipeline_thread, kwargs={"direction": direction}, daemon=True)
    t.start()
    return JSONResponse({"started": True, "message": "Pipeline started", "direction": direction})


@app.get("/run/stream")
async def run_stream(_: None = Depends(require_auth)):
    """
    Server-Sent Events stream. Client connects and receives log lines in real time.
    Closes automatically when pipeline finishes.
    """
    import asyncio

    async def event_generator():
        idx = 0
        while True:
            new_lines, still_running = run_state.snapshot(idx)
            for line in new_lines:
                # Escape for SSE: replace newlines
                safe = line.replace("\n", " ")
                yield f"data: {safe}\n\n"
                idx += 1

            if not still_running and idx >= len(run_state.logs):
                yield "data: __DONE__\n\n"
                break

            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(require_auth)):
    articles = get_pending_articles()

    if not articles:
        articles_html = """
        <div class="empty">
          <h2>No articles yet</h2>
          <p>Click "Force Run Pipeline" to generate your first articles, or wait for the 8 AM daily run.</p>
        </div>"""
    else:
        cards = ""
        for a in articles:
            img_badge = '<span class="badge badge-cyan">Image ✓</span>' if a["has_image"] else '<span class="badge badge-gray">No image</span>'
            li_badge = '<span class="badge badge-violet">LinkedIn ✓</span>' if a["has_linkedin"] else '<span class="badge badge-gray">No LinkedIn</span>'
            pub_badge = '<span class="badge badge-green">Published ✓</span>' if a["is_published"] else ''

            # Hero image block — thumbnail + download link
            if a["has_image"]:
                image_block = f"""
              <div style="margin-bottom:16px;">
                <img src="/image/{a['slug']}" alt="Hero image"
                     style="width:100%;border-radius:6px;border:1px solid #2d3748;display:block;margin-bottom:8px;">
                <a href="/image/{a['slug']}" download="{a['slug']}.png"
                   class="btn btn-secondary" style="font-size:12px;padding:6px 14px;">
                  ⬇ Download Image (LinkedIn)
                </a>
              </div>"""
            else:
                image_block = ""

            # Publish button — locked if already published
            if a["is_published"]:
                publish_btn = '<button class="btn btn-publish" disabled style="background:#1e4d2b;color:#4ade80;cursor:not-allowed;">✅ Published</button>'
            else:
                publish_btn = f'<button onclick="publishArticle(\'{a["slug"]}\', this)" class="btn btn-publish">🚀 Publish to Website</button>'

            cards += f"""
            <div class="card" id="card-{a['slug']}">
              <div class="icons">{img_badge} {li_badge} {pub_badge}</div>
              <h2>{a['title']}</h2>
              <div class="meta">{a['slug']} &middot; {a['read_time']} &middot; Generated {a['modified']}</div>
              <div class="abstract">{a['abstract']}</div>
              {image_block}
              <div class="actions">
                <a href="/article/{a['slug']}" class="btn btn-secondary">Preview &rarr;</a>
                {publish_btn}
                <button onclick="deleteArticle('{a['slug']}', this)" class="btn btn-secondary"
                  style="color:#fca5a5;border-color:#7f1d1d;" title="Delete from dashboard">
                  🗑 Delete
                </button>
              </div>
              <div id="result-{a['slug']}" style="margin-top:12px;"></div>
            </div>"""
        articles_html = cards

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
      <p>{len(articles)} article(s) pending review &middot; Next auto-run: 8:00 AM CT daily</p>
    </div>
    <div style="display:flex;gap:10px;align-items:center;">
      <span class="badge badge-cyan">Marketing Agent</span>
    </div>
  </div>

  <div class="layout">
    <!-- Left: articles -->
    <div>
      {articles_html}
    </div>

    <!-- Right: console panel -->
    <div>
      <div class="console-panel">
        <div class="console-header">
          <h3>Pipeline Console</h3>
          <span id="console-status" class="console-status idle">Idle</span>
        </div>
        <div class="console-body" id="console-body">
          <span class="console-empty">Run the pipeline to see live output here.</span>
        </div>
        <div class="console-footer">
          <div class="direction-wrap">
            <label class="direction-label" for="direction-input">Article direction (optional)</label>
            <textarea id="direction-input" class="direction-input"
              placeholder="e.g. 'How MPI handles ADAS calibration on hail claims' or 'OEM sectioning rules for high-strength steel on SGI repairs' — leave blank for autonomous topic selection"></textarea>
            <div class="direction-hint">Leave blank → agent picks topics autonomously</div>
          </div>
          <button id="run-btn" onclick="forceRun(this)" class="btn btn-run" style="width:100%">
            ⚡ Force Run Pipeline
          </button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let es = null;

function forceRun(btn) {{
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add('running');

  const directionEl = document.getElementById('direction-input');
  const direction = directionEl ? directionEl.value.trim() : '';

  btn.textContent = direction ? '⏳ Running (directed)...' : '⏳ Running...';

  const body = document.getElementById('console-body');
  const status = document.getElementById('console-status');
  body.innerHTML = '';
  status.className = 'console-status running';
  status.textContent = 'Running';

  if (direction) {{
    appendLog('Direction: ' + direction);
  }}

  // Start the pipeline
  fetch('/run', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ direction: direction }}),
  }})
    .then(r => r.json())
    .then(data => {{
      if (!data.started && data.message.includes('already')) {{
        appendLog('Pipeline is already running — connecting to stream...');
      }}
      startStream(btn);
    }})
    .catch(e => {{
      appendLog('ERROR: Could not start pipeline — ' + e.message);
      setDone(btn, false);
    }});
}}

function startStream(btn) {{
  if (es) es.close();
  es = new EventSource('/run/stream');

  es.onmessage = (e) => {{
    if (e.data === '__DONE__') {{
      es.close();
      setDone(btn, true);
      // Reload page after 3s so new articles appear
      setTimeout(() => location.reload(), 3000);
      return;
    }}
    appendLog(e.data);
  }};

  es.onerror = () => {{
    es.close();
    appendLog('Stream disconnected.');
    setDone(btn, false);
  }};
}}

function appendLog(line) {{
  const body = document.getElementById('console-body');
  const div = document.createElement('div');
  div.className = 'log-line';

  // Colour-code special lines
  let cls = 'log-text';
  if (line.includes('ERROR') || line.includes('error')) cls += ' err';
  else if (line.includes('STEP ') || line.includes('===')) cls += ' step';
  else if (line.includes('Done') || line.includes('complete') || line.includes('✓')) cls += ' done';

  div.innerHTML = '<span class="' + cls + '">' + escHtml(line) + '</span>';
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}}

function setDone(btn, success) {{
  const status = document.getElementById('console-status');
  status.className = 'console-status ' + (success ? 'done' : 'error');
  status.textContent = success ? 'Done' : 'Error';
  btn.disabled = false;
  btn.textContent = '⚡ Force Run Pipeline';
  btn.classList.remove('running');
  if (success) appendLog('✓ Pipeline complete — reloading in 3 seconds...');
}}

function escHtml(t) {{
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

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
      // Lock the button permanently
      btn.textContent = '✅ Published';
      btn.style.background = '#1e4d2b';
      btn.style.color = '#4ade80';
      btn.style.cursor = 'not-allowed';
      btn.onclick = null;
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

async function deleteArticle(slug, btn) {{
  if (!confirm('Delete "' + slug + '" from the dashboard? This removes the local files but does NOT unpublish it from the website.')) return;
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  try {{
    const resp = await fetch('/article/' + slug, {{ method: 'DELETE' }});
    const data = await resp.json();
    if (data.success) {{
      const card = document.getElementById('card-' + slug);
      if (card) {{
        card.style.transition = 'opacity 0.3s';
        card.style.opacity = '0';
        setTimeout(() => card.remove(), 300);
      }}
    }} else {{
      alert('Delete failed: ' + data.message);
      btn.disabled = false;
      btn.textContent = '🗑 Delete';
    }}
  }} catch(e) {{
    alert('Network error: ' + e.message);
    btn.disabled = false;
    btn.textContent = '🗑 Delete';
  }}
}}

// On load: if pipeline is already running, auto-connect to stream
fetch('/run/status')
  .then(r => r.json())
  .then(data => {{
    if (data.running) {{
      const btn = document.getElementById('run-btn');
      btn.disabled = true;
      btn.textContent = '⏳ Running...';
      btn.classList.add('running');
      document.getElementById('console-status').className = 'console-status running';
      document.getElementById('console-status').textContent = 'Running';
      startStream(btn);
    }}
  }});
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

    ts_path = OUTPUT_DIR / "articles" / f"{slug}.ts"
    ts_code = ts_path.read_text(encoding="utf-8") if ts_path.exists() else "(file not found)"

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
    {'<a href="/image/' + slug + '" download="' + slug + '.png" class="btn btn-secondary">⬇ Download Image</a>' if article["has_image"] else ''}
  </div>

  {'<img src="/image/' + slug + '" alt="Hero image" style="width:100%;border-radius:8px;border:1px solid #2d3748;margin-bottom:24px;">' if article["has_image"] else ''}

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


@app.get("/image/{slug}")
def download_image(slug: str, _: None = Depends(require_auth)):
    """Serve the hero image as a downloadable PNG."""
    from fastapi.responses import FileResponse
    img_path = OUTPUT_DIR / "images" / f"{slug}.png"
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        path=str(img_path),
        media_type="image/png",
        filename=f"{slug}.png",
        headers={"Content-Disposition": f'attachment; filename="{slug}.png"'},
    )


@app.delete("/article/{slug}")
def remove_article(slug: str, _: None = Depends(require_auth)):
    result = delete_article(slug)
    return JSONResponse(content=result)


@app.post("/publish/{slug}")
def publish(slug: str, _: None = Depends(require_auth)):
    result = publish_article(slug)
    return JSONResponse(content=result)
