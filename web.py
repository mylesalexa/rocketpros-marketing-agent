"""
RocketPros Marketing Super Hub — Web Dashboard
Runs alongside the scheduler on Railway PRO.

Tabs:
  Drafts        — pending local articles (edit, publish, delete)
  Live Site     — articles currently on rocketpros.app (sync, pull for edit)
  LinkedIn Hub  — all LinkedIn post variants (copy, regenerate)
  Analytics     — pipeline history, publication stats
  Run Pipeline  — trigger runs + live SSE console

API Routes:
  GET  /api/sync                            Trigger GitHub sync
  GET  /api/site-articles                   Cached live articles JSON
  GET  /api/stats                           Pipeline/publication stats
  GET  /api/article/{slug}/preview          Parsed paper dict for editor
  POST /api/article/{slug}/edit             Save edited paper dict
  POST /api/article/{slug}/rename           Rename slug + title + files
  POST /api/article/{slug}/regenerate-linkedin  Regenerate LinkedIn posts
  POST /api/sync/pull/{slug}                Copy live article to drafts

Legacy Routes (preserved):
  GET  /                  Dashboard (now 5-tab layout)
  GET  /article/{slug}    Article detail redirect to editor
  POST /publish/{slug}    Publish to GitHub
  DELETE /article/{slug}  Delete local files
  GET  /image/{slug}      Download hero image as PNG
  GET  /run/stream        SSE live pipeline logs
  GET  /run/status        Pipeline status JSON
  POST /run               Force-trigger pipeline
  GET  /health            Health check
"""

import os
import sys
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from modules.publisher import publish_article, get_pending_articles, get_linkedin_posts, delete_article, is_published
from modules.auth import (
    init_users, verify_user, list_users, create_user,
    change_password, change_role, delete_user,
)

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-in-production-please")
SITE_URL = os.getenv("SITE_URL", "https://rocketpros.app")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/output"))

app = FastAPI(title="RocketPros Marketing Super Hub", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="rp_session", max_age=86400 * 7)

# Bootstrap users on startup
init_users(OUTPUT_DIR)


# ── Run state ──────────────────────────────────────────────────────────────────

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


class RunRequest(BaseModel):
    direction: str = ""


class LogCapture:
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

def require_auth(request: Request) -> dict:
    """Dependency: returns session user dict or raises 401 redirect."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


def require_admin(request: Request) -> dict:
    """Dependency: requires admin role."""
    user = require_auth(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Pydantic models ────────────────────────────────────────────────────────────

class ArticleEditRequest(BaseModel):
    paper: dict
    ts_raw: Optional[str] = None


class RenameRequest(BaseModel):
    new_title: str


class RegenerateSectionRequest(BaseModel):
    section_index: int
    instruction: str = ""


# ── CSS ────────────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; min-height: 100vh; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 28px 16px; }

/* Header */
.header { border-bottom: 2px solid #06b6d4; padding-bottom: 18px; margin-bottom: 24px;
          display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.header h1 { color: #06b6d4; font-size: 20px; font-weight: 700; }
.header p { color: #64748b; font-size: 13px; margin-top: 4px; }

/* Tabs */
.tabs { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #2d3748; padding-bottom: 0; }
.tab-btn { background: none; border: none; color: #64748b; font-size: 14px; font-weight: 600;
           padding: 10px 18px; cursor: pointer; border-bottom: 2px solid transparent;
           margin-bottom: -1px; transition: color .15s, border-color .15s; white-space: nowrap; }
.tab-btn:hover { color: #94a3b8; }
.tab-btn.active { color: #06b6d4; border-bottom-color: #06b6d4; }
.tab-count { background: #1e293b; color: #64748b; border-radius: 10px;
             font-size: 11px; padding: 1px 7px; margin-left: 6px; }
.tab-count.has-items { background: #0e4a5a; color: #06b6d4; }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Badges */
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px;
         font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.badge-cyan { background: #06b6d4; color: #0f1117; }
.badge-violet { background: #8b5cf6; color: #fff; }
.badge-green { background: #16a34a; color: #fff; }
.badge-gray { background: #374151; color: #9ca3af; }
.badge-orange { background: #c2410c; color: #fff; }
.badge-amber { background: #92400e; color: #fbbf24; }
.badge-blue { background: #1e3a5f; color: #60a5fa; }

/* Cards */
.card { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 10px;
        padding: 22px; margin-bottom: 18px; }
.card h2 { font-size: 16px; font-weight: 700; color: #f1f5f9; margin-bottom: 5px; }
.card .meta { color: #64748b; font-size: 12px; margin-bottom: 10px; }
.card .abstract { color: #94a3b8; font-size: 13px; line-height: 1.6; margin-bottom: 14px; }
.icons { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }

/* Buttons */
.btn { display: inline-block; padding: 7px 16px; border-radius: 6px; font-size: 13px;
       font-weight: 600; cursor: pointer; border: none; text-decoration: none;
       transition: opacity .15s; line-height: 1.4; }
.btn:hover { opacity: .85; }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.btn-primary { background: #06b6d4; color: #0f1117; }
.btn-secondary { background: #1e293b; color: #94a3b8; border: 1px solid #374151; }
.btn-edit { background: #7c3aed; color: #fff; }
.btn-publish { background: #16a34a; color: #fff; }
.btn-run { background: #7c3aed; color: #fff; }
.btn-run.running { background: #374151; }
.btn-delete { background: #1e293b; color: #fca5a5; border: 1px solid #7f1d1d; }
.btn-delete:hover { background: #7f1d1d; color: #fff; opacity: 1; }
.btn-danger { background: #dc2626; color: #fff; }
.btn-sm { padding: 5px 12px; font-size: 12px; }
.btn-copy { background: #1e293b; color: #06b6d4; border: 1px solid #164e63; font-size: 12px; padding: 5px 12px; }
.btn-copy:hover { background: #164e63; }

/* Empty state */
.empty { text-align: center; padding: 60px 0; color: #475569; }
.empty h2 { font-size: 18px; margin-bottom: 8px; color: #64748b; }

/* Banners */
.success-banner { background: #052e16; border: 1px solid #16a34a; border-radius: 8px;
                  padding: 14px; margin-bottom: 16px; color: #4ade80; font-size: 14px; }
.error-banner { background: #450a0a; border: 1px solid #dc2626; border-radius: 8px;
                padding: 14px; margin-bottom: 16px; color: #fca5a5; font-size: 14px; }
.info-banner { background: #0c1a2e; border: 1px solid #1e40af; border-radius: 8px;
               padding: 14px; margin-bottom: 16px; color: #93c5fd; font-size: 14px; }
.warn-banner { background: #1c1000; border: 1px solid #92400e; border-radius: 8px;
               padding: 14px; margin-bottom: 16px; color: #fbbf24; font-size: 13px; }

/* Console panel */
.console-panel { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 10px;
                 padding: 0; overflow: hidden; }
.console-header { background: #0f1117; padding: 12px 16px; border-bottom: 1px solid #2d3748;
                  display: flex; align-items: center; justify-content: space-between; }
.console-header h3 { font-size: 12px; font-weight: 700; color: #94a3b8;
                     text-transform: uppercase; letter-spacing: .08em; }
.console-status { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 20px;
                  text-transform: uppercase; letter-spacing: .06em; }
.console-status.idle { background: #1e293b; color: #475569; }
.console-status.running { background: #7c3aed22; color: #a78bfa; animation: pulse 1.5s ease-in-out infinite; }
.console-status.done { background: #052e16; color: #4ade80; }
.console-status.error { background: #450a0a; color: #fca5a5; }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
.console-body { font-family: 'Courier New', monospace; font-size: 11px; line-height: 1.6;
                color: #94a3b8; padding: 12px 16px; height: 380px; overflow-y: auto;
                background: #0a0d16; }
.log-line { margin-bottom: 2px; }
.log-text { color: #a5f3fc; }
.log-text.err { color: #fca5a5; }
.log-text.done { color: #4ade80; font-weight: 700; }
.log-text.step { color: #f59e0b; font-weight: 700; }
.console-empty { color: #374151; font-style: italic; }
.console-footer { padding: 14px 16px; border-top: 1px solid #2d3748; }
.direction-label { font-size: 11px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: .08em; color: #64748b; margin-bottom: 6px; display: block; }
.direction-hint { font-size: 11px; color: #374151; margin-top: 4px; }
textarea.direction-input { width: 100%; background: #0a0d16; border: 1px solid #374151;
  border-radius: 6px; color: #e2e8f0; font-size: 12px; padding: 10px 12px;
  resize: vertical; min-height: 72px; font-family: inherit; line-height: 1.5; }
textarea.direction-input:focus { outline: none; border-color: #7c3aed; }
textarea.direction-input::placeholder { color: #374151; }

/* Pre / code blocks */
pre { background: #0a0d16; border: 1px solid #2d3748; border-radius: 8px; padding: 14px;
      font-size: 11px; color: #a5f3fc; overflow-x: auto; white-space: pre-wrap;
      max-height: 380px; overflow-y: auto; }

/* LinkedIn blocks */
.linkedin-block { background: #0f1117; border: 1px solid #2d3748; border-radius: 8px;
                  padding: 14px; font-size: 13px; color: #cbd5e1; line-height: 1.7; white-space: pre-wrap; }
.section-label { font-size: 11px; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .1em; color: #8b5cf6; margin: 20px 0 8px 0;
                 padding-top: 16px; border-top: 1px solid #2d3748; }

/* Sync bar */
.sync-bar { display: flex; align-items: center; gap: 12px; padding: 10px 14px;
            background: #0f1117; border: 1px solid #2d3748; border-radius: 8px;
            margin-bottom: 18px; font-size: 12px; color: #64748b; }
.sync-bar .sync-time { flex: 1; }
.sync-dot { width: 8px; height: 8px; border-radius: 50%; background: #16a34a;
            flex-shrink: 0; }
.sync-dot.stale { background: #92400e; }
.sync-dot.never { background: #374151; }

/* Stat cards */
.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr)); gap: 16px; margin-bottom: 24px; }
.stat-card { background: #1a1f2e; border: 1px solid #2d3748; border-radius: 10px; padding: 20px; }
.stat-card .stat-num { font-size: 36px; font-weight: 800; color: #06b6d4; line-height: 1; }
.stat-card .stat-label { font-size: 12px; color: #64748b; margin-top: 6px; text-transform: uppercase; letter-spacing: .06em; }

/* Analytics table */
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th { text-align: left; padding: 8px 12px; color: #64748b; font-size: 11px;
                 text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid #2d3748; }
.data-table td { padding: 9px 12px; border-bottom: 1px solid #1e293b; color: #94a3b8; }
.data-table tr:hover td { background: #1a1f2e; }
.data-table a { color: #06b6d4; text-decoration: none; }
.data-table a:hover { text-decoration: underline; }

/* LinkedIn accordion */
.li-accordion { border: 1px solid #2d3748; border-radius: 8px; overflow: hidden; margin-bottom: 12px; }
.li-accordion-header { background: #1a1f2e; padding: 12px 16px; cursor: pointer;
                       display: flex; align-items: center; justify-content: space-between;
                       font-size: 14px; font-weight: 600; color: #e2e8f0; }
.li-accordion-header:hover { background: #1e293b; }
.li-accordion-body { display: none; border-top: 1px solid #2d3748; }
.li-variant { padding: 14px 16px; border-bottom: 1px solid #1e293b; }
.li-variant:last-child { border-bottom: none; }
.li-variant-label { font-size: 11px; font-weight: 700; color: #8b5cf6; text-transform: uppercase;
                    letter-spacing: .08em; margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; }

/* Modal */
.modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
                 z-index: 1000; overflow-y: auto; padding: 24px 16px; }
.modal-content { background: #0f1117; border: 1px solid #2d3748; border-radius: 12px;
                 max-width: 900px; margin: 0 auto; overflow: hidden; }
.modal-header { background: #1a1f2e; padding: 18px 24px; border-bottom: 1px solid #2d3748;
                display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.modal-header h2 { font-size: 16px; font-weight: 700; color: #f1f5f9; flex: 1; min-width: 200px; }
.modal-body { padding: 24px; max-height: 70vh; overflow-y: auto; }
.modal-footer { background: #1a1f2e; padding: 14px 24px; border-top: 1px solid #2d3748;
                display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }

/* Form fields */
.field-group { margin-bottom: 18px; }
.field-label { display: block; font-size: 11px; font-weight: 700; text-transform: uppercase;
               letter-spacing: .08em; color: #64748b; margin-bottom: 6px; }
.field-input { width: 100%; background: #0a0d16; border: 1px solid #374151; border-radius: 6px;
               color: #e2e8f0; font-size: 13px; padding: 9px 12px; font-family: inherit;
               transition: border-color .15s; }
.field-input:focus { outline: none; border-color: #06b6d4; }
.field-input.title-input { font-size: 17px; font-weight: 600; color: #f1f5f9; }
.field-textarea { width: 100%; background: #0a0d16; border: 1px solid #374151; border-radius: 6px;
                  color: #e2e8f0; font-size: 13px; padding: 9px 12px; font-family: inherit;
                  line-height: 1.6; resize: vertical; }
.field-textarea:focus { outline: none; border-color: #06b6d4; }
.field-select { width: 100%; background: #0a0d16; border: 1px solid #374151; border-radius: 6px;
                color: #e2e8f0; font-size: 13px; padding: 9px 12px; }
.field-select:focus { outline: none; border-color: #06b6d4; }
.field-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap: 14px; }
.slug-display { font-size: 11px; color: #475569; margin-top: 5px; font-family: monospace; }
.slug-display.changed { color: #fbbf24; }

/* Dynamic list items */
.list-item { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 8px; }
.list-item textarea { flex: 1; }
.list-item .btn-sm { flex-shrink: 0; margin-top: 2px; }

/* Section editor */
.section-row { background: #0f1117; border: 1px solid #2d3748; border-radius: 8px;
               padding: 14px; margin-bottom: 10px; }
.section-row-header { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.section-type-badge { font-size: 10px; font-weight: 800; padding: 2px 8px; border-radius: 4px;
                      background: #1e293b; color: #8b5cf6; text-transform: uppercase;
                      letter-spacing: .08em; flex-shrink: 0; }
.section-row-actions { margin-left: auto; display: flex; gap: 6px; }
.table-headers-input { font-family: monospace; font-size: 12px; }
.table-rows-input { font-family: monospace; font-size: 11px; min-height: 120px; }

/* Spinner */
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #374151;
           border-top-color: #06b6d4; border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Misc */
.back { color: #06b6d4; text-decoration: none; font-size: 13px; }
.back:hover { text-decoration: underline; }
.img-thumb { width: 100%; border-radius: 6px; border: 1px solid #2d3748; display: block; margin-bottom: 10px; }
.section-divider { border-top: 1px solid #2d3748; margin: 20px 0; }
.filter-input { background: #0a0d16; border: 1px solid #374151; border-radius: 6px;
                color: #e2e8f0; font-size: 13px; padding: 8px 12px; width: 100%; max-width: 360px; }
.filter-input:focus { outline: none; border-color: #06b6d4; }
"""


# ── Pipeline runner ────────────────────────────────────────────────────────────

def _run_pipeline_thread(direction: str = ""):
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_published() -> dict:
    p = OUTPUT_DIR / "published.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_last_run() -> dict:
    p = OUTPUT_DIR / "last_run.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _esc(t: str) -> str:
    """HTML-escape for safe insertion."""
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ── Article card HTML ──────────────────────────────────────────────────────────

def _draft_card_html(a: dict) -> str:
    img_badge = '<span class="badge badge-cyan">Image ✓</span>' if a["has_image"] else '<span class="badge badge-gray">No image</span>'
    li_badge = '<span class="badge badge-violet">LinkedIn ✓</span>' if a["has_linkedin"] else '<span class="badge badge-gray">No LinkedIn</span>'
    pub_badge = '<span class="badge badge-green">Published ✓</span>' if a["is_published"] else ''

    image_block = ""
    if a["has_image"]:
        image_block = f"""
        <div style="margin-bottom:14px;">
          <img src="/image/{a['slug']}" class="img-thumb" alt="Hero">
          <a href="/image/{a['slug']}" download="{a['slug']}.png" class="btn btn-secondary btn-sm">&#x2B07; Download Image</a>
        </div>"""

    if a["is_published"]:
        publish_btn = f'<button class="btn btn-publish" disabled style="background:#1e4d2b;color:#4ade80;cursor:not-allowed;">&#x2705; Published</button>'
        live_link = f'<a href="{SITE_URL}/research/{a["slug"]}" target="_blank" class="btn btn-secondary btn-sm">View Live &rarr;</a>'
    else:
        publish_btn = f'<button onclick="publishArticle(\'{a["slug"]}\', this)" class="btn btn-publish">&#x1F680; Publish</button>'
        live_link = ""

    return f"""
    <div class="card" id="card-{a['slug']}">
      <div class="icons">{img_badge} {li_badge} {pub_badge}</div>
      <h2>{_esc(a['title'])}</h2>
      <div class="meta">{_esc(a['slug'])} &middot; {_esc(a['read_time'])} &middot; {_esc(a['modified'])}</div>
      <div class="abstract">{_esc(a['abstract'])}</div>
      {image_block}
      <div class="actions">
        <button onclick="openEditor('{a['slug']}', 'local')" class="btn btn-edit">&#x270F; Edit</button>
        {publish_btn}
        {live_link}
        <button onclick="deleteArticle('{a['slug']}', this, {str(a['is_published']).lower()})" class="btn btn-delete btn-sm" title="Delete from dashboard">&#x1F5D1; Delete</button>
      </div>
      <div id="result-{a['slug']}" style="margin-top:10px;"></div>
    </div>"""


# ── Admin tab HTML ────────────────────────────────────────────────────────────

def _build_admin_tab_html() -> str:
    return """
    <div style="max-width:700px;">
      <div class="card">
        <h2 style="margin-bottom:16px;color:#06b6d4;">&#x1F464; User Management</h2>
        <div id="users-list">
          <div class="empty"><h2><span class="spinner"></span> Loading...</h2></div>
        </div>
        <hr style="border-color:#2d3748;margin:20px 0;">
        <h3 style="font-size:14px;color:#94a3b8;margin-bottom:14px;">Add New User</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr 120px;gap:10px;align-items:end;">
          <div>
            <label class="field-label">Username</label>
            <input type="text" class="field-input" id="new-username" placeholder="e.g. ali" autocomplete="off">
          </div>
          <div>
            <label class="field-label">Password</label>
            <input type="password" class="field-input" id="new-password" placeholder="Strong password" autocomplete="new-password">
          </div>
          <div>
            <label class="field-label">Role</label>
            <select class="field-input" id="new-role" style="padding:8px 10px;">
              <option value="viewer">Viewer</option>
              <option value="admin">Admin</option>
            </select>
          </div>
        </div>
        <button onclick="addUser()" class="btn btn-primary" style="margin-top:12px;">&#x2795; Add User</button>
        <div id="user-add-result" style="margin-top:10px;"></div>
      </div>
    </div>"""


# ── Main dashboard HTML ────────────────────────────────────────────────────────

def _build_dashboard_html(articles: list[dict], current_user: dict | None = None) -> str:
    draft_count = len(articles)
    draft_count_cls = "has-items" if draft_count > 0 else ""
    username = (current_user or {}).get("username", "")
    role = (current_user or {}).get("role", "viewer")
    is_admin = role == "admin"
    admin_tab = '<button class="tab-btn" onclick="switchTab(\'admin\', this)">&#x1F464; Users</button>' if is_admin else ""

    if not articles:
        drafts_content = """
        <div class="empty">
          <h2>No drafts yet</h2>
          <p>Click the Run Pipeline tab to generate your first articles, or wait for the 8 AM daily run.</p>
        </div>"""
    else:
        drafts_content = "".join(_draft_card_html(a) for a in articles)

    admin_tab_content = _build_admin_tab_html() if is_admin else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RocketPros Marketing Super Hub</title>
  <style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div>
      <h1>&#x1F680; RocketPros Marketing Super Hub</h1>
      <p>{draft_count} draft(s) pending &middot; {SITE_URL}</p>
    </div>
    <div style="display:flex;align-items:center;gap:12px;">
      <span class="badge badge-cyan">Marketing Agent</span>
      <span style="font-size:12px;color:#64748b;">&#x1F464; {_esc(username)}</span>
      <a href="/logout" style="font-size:12px;color:#94a3b8;text-decoration:none;" title="Sign out">Sign out</a>
    </div>
  </div>

  <!-- Tab nav -->
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('drafts', this)">
      Drafts <span class="tab-count {draft_count_cls}" id="tc-drafts">{draft_count}</span>
    </button>
    <button class="tab-btn" onclick="switchTab('live', this)">
      Live Site <span class="tab-count" id="tc-live">...</span>
    </button>
    <button class="tab-btn" onclick="switchTab('linkedin', this)">LinkedIn Hub</button>
    <button class="tab-btn" onclick="switchTab('analytics', this)">Analytics</button>
    <button class="tab-btn" onclick="switchTab('pipeline', this)">
      Run Pipeline <span id="pipeline-dot" style="display:none;width:8px;height:8px;background:#a78bfa;border-radius:50%;display:none;margin-left:6px;"></span>
    </button>
    {admin_tab}
  </div>

  <!-- TAB: Drafts -->
  <div class="tab-content active" id="tab-drafts">
    {drafts_content}
  </div>

  <!-- TAB: Live Site -->
  <div class="tab-content" id="tab-live">
    <div class="sync-bar" id="sync-bar">
      <div class="sync-dot never" id="sync-dot"></div>
      <div class="sync-time" id="sync-time">Not yet synced</div>
      <button onclick="runSync()" class="btn btn-secondary btn-sm" id="sync-btn">&#x21BB; Sync Now</button>
    </div>
    <div id="live-articles">
      <div class="empty"><h2>Click Sync Now to load live articles</h2></div>
    </div>
  </div>

  <!-- TAB: LinkedIn Hub -->
  <div class="tab-content" id="tab-linkedin">
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:18px;">
      <input type="text" class="filter-input" id="li-filter" placeholder="Filter by article title..." oninput="filterLinkedin(this.value)">
      <span id="li-count" style="color:#64748b;font-size:13px;white-space:nowrap;"></span>
    </div>
    <div id="linkedin-list">
      <div class="empty"><h2>Loading...</h2></div>
    </div>
  </div>

  <!-- TAB: Analytics -->
  <div class="tab-content" id="tab-analytics">
    <div id="analytics-content">
      <div class="empty"><h2>Loading...</h2></div>
    </div>
  </div>

  <!-- TAB: Run Pipeline -->
  <div class="tab-content" id="tab-pipeline">
    <div style="max-width:640px;">
      <div class="console-panel">
        <div class="console-header">
          <h3>Pipeline Console</h3>
          <span id="console-status" class="console-status idle">Idle</span>
        </div>
        <div class="console-body" id="console-body">
          <span class="console-empty">Run the pipeline to see live output here.</span>
        </div>
        <div class="console-footer">
          <div style="margin-bottom:12px;">
            <label class="direction-label" for="direction-input">Article direction (optional)</label>
            <textarea id="direction-input" class="direction-input"
              placeholder="e.g. 'How MPI handles ADAS calibration on hail claims' — leave blank for autonomous topic selection"></textarea>
            <div class="direction-hint">Leave blank &rarr; agent picks topics autonomously</div>
          </div>
          <button id="run-btn" onclick="forceRun(this)" class="btn btn-run" style="width:100%">
            &#x26A1; Force Run Pipeline
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB: Admin Users (admin only) -->
  <div class="tab-content" id="tab-admin">
    {admin_tab_content}
  </div>

</div><!-- end .wrap -->

<!-- ── Article Editor Modal ──────────────────────────────────────────────── -->
<div class="modal-overlay" id="editor-modal" onclick="handleModalClick(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h2 id="modal-title">Edit Article</h2>
      <div style="display:flex;gap:8px;">
        <button onclick="saveArticle()" class="btn btn-primary" id="modal-save-btn">Save Draft</button>
        <button onclick="saveAndPublish()" class="btn btn-publish" id="modal-publish-btn">Save &amp; Publish</button>
        <button onclick="closeEditor()" class="btn btn-secondary">Cancel</button>
      </div>
    </div>

    <div class="modal-body" id="editor-body">

      <!-- Banner area -->
      <div id="editor-banner"></div>

      <!-- Slug warning -->
      <div class="warn-banner" id="slug-warning" style="display:none;">
        &#x26A0; The title change will rename this article's slug.
        The article will need to be re-published after saving.
        <label style="display:flex;align-items:center;gap:8px;margin-top:8px;cursor:pointer;">
          <input type="checkbox" id="slug-confirm-check" onchange="checkSlugConfirm()">
          I understand — rename the article
        </label>
      </div>

      <!-- Title + slug -->
      <div class="field-group">
        <label class="field-label">Title</label>
        <input type="text" class="field-input title-input" id="e-title" oninput="onTitleChange(this.value)">
        <div class="slug-display" id="e-slug-display"></div>
      </div>

      <!-- Subtitle -->
      <div class="field-group">
        <label class="field-label">Subtitle</label>
        <input type="text" class="field-input" id="e-subtitle">
      </div>

      <!-- Metadata row -->
      <div class="field-row">
        <div class="field-group">
          <label class="field-label">Category</label>
          <input type="text" class="field-input" id="e-category">
        </div>
        <div class="field-group">
          <label class="field-label">Audience</label>
          <input type="text" class="field-input" id="e-audience">
        </div>
        <div class="field-group">
          <label class="field-label">Region</label>
          <select class="field-select" id="e-region">
            <option value="Canada">Canada</option>
            <option value="United States">United States</option>
            <option value="North America">North America</option>
          </select>
        </div>
        <div class="field-group">
          <label class="field-label">Read Time</label>
          <input type="text" class="field-input" id="e-readTime" placeholder="13 min read">
        </div>
        <div class="field-group">
          <label class="field-label">Published Date</label>
          <input type="date" class="field-input" id="e-published">
        </div>
      </div>

      <!-- Abstract -->
      <div class="field-group">
        <label class="field-label">Abstract</label>
        <textarea class="field-textarea" id="e-abstract" rows="5"></textarea>
      </div>

      <div class="section-divider"></div>

      <!-- Key Findings -->
      <div class="field-group">
        <label class="field-label">Key Findings</label>
        <div id="e-keyFindings-list"></div>
        <button onclick="addListItem('keyFindings')" class="btn btn-secondary btn-sm" style="margin-top:6px;">+ Add Finding</button>
      </div>

      <div class="section-divider"></div>

      <!-- Sections -->
      <div class="field-group">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
          <label class="field-label" style="margin:0;">Content Sections</label>
          <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;">
            <button onclick="addSection('h2')" class="btn btn-secondary btn-sm">+ H2</button>
            <button onclick="addSection('h3')" class="btn btn-secondary btn-sm">+ H3</button>
            <button onclick="addSection('p')" class="btn btn-secondary btn-sm">+ Para</button>
            <button onclick="addSection('ul')" class="btn btn-secondary btn-sm">+ List</button>
            <button onclick="addSection('table')" class="btn btn-secondary btn-sm">+ Table</button>
            <button onclick="addSection('callout')" class="btn btn-secondary btn-sm">+ Callout</button>
          </div>
        </div>
        <div id="e-sections-list"></div>
      </div>

      <div class="section-divider"></div>

      <!-- Shop Implications -->
      <div class="field-group">
        <label class="field-label">Shop Implications</label>
        <div id="e-shopImplications-list"></div>
        <button onclick="addListItem('shopImplications')" class="btn btn-secondary btn-sm" style="margin-top:6px;">+ Add Implication</button>
      </div>

      <!-- Carrier Implications -->
      <div class="field-group">
        <label class="field-label">Carrier Implications</label>
        <div id="e-carrierImplications-list"></div>
        <button onclick="addListItem('carrierImplications')" class="btn btn-secondary btn-sm" style="margin-top:6px;">+ Add Implication</button>
      </div>

      <div class="section-divider"></div>

      <!-- FAQ -->
      <div class="field-group">
        <label class="field-label">FAQ</label>
        <div id="e-faq-list"></div>
        <button onclick="addFaq()" class="btn btn-secondary btn-sm" style="margin-top:6px;">+ Add FAQ</button>
      </div>

      <div class="section-divider"></div>

      <!-- Citations -->
      <div class="field-group">
        <label class="field-label">Citations</label>
        <div id="e-citations-list"></div>
        <button onclick="addCitation()" class="btn btn-secondary btn-sm" style="margin-top:6px;">+ Add Citation</button>
      </div>

      <div class="section-divider"></div>

      <!-- LinkedIn Posts -->
      <div class="field-group">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
          <label class="field-label" style="margin:0;">LinkedIn Posts</label>
          <button onclick="regenLinkedin()" class="btn btn-secondary btn-sm" id="regen-li-btn" style="margin-left:auto;">&#x21BA; Regenerate All</button>
        </div>
        <div id="li-editor">
          <div style="margin-bottom:12px;">
            <div class="li-variant-label">Hook Post (Myles)</div>
            <textarea class="field-textarea" id="li-hook" rows="8"></textarea>
          </div>
          <div style="margin-bottom:12px;">
            <div class="li-variant-label">Insight Post (Ali)</div>
            <textarea class="field-textarea" id="li-insight" rows="8"></textarea>
          </div>
          <div>
            <div class="li-variant-label">Story Post (Myles)</div>
            <textarea class="field-textarea" id="li-story" rows="8"></textarea>
          </div>
        </div>
      </div>

    </div><!-- end modal-body -->

    <div class="modal-footer">
      <button onclick="saveArticle()" class="btn btn-primary">Save Draft</button>
      <button onclick="saveAndPublish()" class="btn btn-publish">Save &amp; Publish</button>
      <button onclick="regenLinkedin()" class="btn btn-secondary">&#x21BA; Regenerate LinkedIn</button>
      <button onclick="closeEditor()" class="btn btn-secondary" style="margin-left:auto;">Cancel</button>
    </div>
  </div>
</div>

<script>
// ── Global editor state ──────────────────────────────────────────────────────
let editorSlug = '';
let editorOrigSlug = '';
let editorSource = 'local';
let editorSections = [];

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name, btn) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'live' && !liveLoaded) loadLiveSite();
  if (name === 'linkedin' && !linkedinLoaded) loadLinkedin();
  if (name === 'analytics' && !analyticsLoaded) loadAnalytics();
  if (name === 'admin' && !adminLoaded) loadUsers();
}}

let liveLoaded = false;
let linkedinLoaded = false;
let analyticsLoaded = false;

// ── Editor open/close ─────────────────────────────────────────────────────────
function openEditor(slug, source) {{
  editorSlug = slug;
  editorOrigSlug = slug;
  editorSource = source || 'local';
  clearEditorBanner();
  document.getElementById('slug-warning').style.display = 'none';
  document.getElementById('slug-confirm-check').checked = false;
  document.getElementById('modal-save-btn').disabled = false;
  document.getElementById('modal-publish-btn').disabled = false;
  document.getElementById('modal-title').textContent = 'Loading...';

  fetch('/api/article/' + slug + '/preview?source=' + editorSource)
    .then(r => r.json())
    .then(data => {{
      if (!data.success) {{ showEditorBanner('error', 'Error loading article: ' + data.message); return; }}
      document.getElementById('modal-title').textContent = 'Edit: ' + (data.paper.title || slug);
      editorSections = JSON.parse(JSON.stringify(data.paper.sections || []));
      populateForm(data.paper);
      populateLinkedinEditor(data.linkedin_posts || {{}});
      document.getElementById('editor-modal').style.display = 'flex';
    }})
    .catch(e => showEditorBanner('error', 'Network error: ' + e.message));
}}

function closeEditor() {{
  document.getElementById('editor-modal').style.display = 'none';
}}

function handleModalClick(e) {{
  if (e.target === document.getElementById('editor-modal')) closeEditor();
}}

// ── Form population ───────────────────────────────────────────────────────────
function populateForm(paper) {{
  setVal('e-title', paper.title || '');
  setVal('e-subtitle', paper.subtitle || '');
  setVal('e-category', paper.category || '');
  setVal('e-audience', paper.audience || '');
  setVal('e-region', paper.region || 'Canada');
  setVal('e-readTime', paper.readTime || '');
  setVal('e-published', paper.published || '');
  setVal('e-abstract', paper.abstract || '');

  const slugEl = document.getElementById('e-slug-display');
  slugEl.textContent = 'slug: ' + (paper.slug || '');
  slugEl.className = 'slug-display';

  renderDynamicList('e-keyFindings-list', paper.keyFindings || [], 'keyFindings');
  renderDynamicList('e-shopImplications-list', paper.shopImplications || [], 'shopImplications');
  renderDynamicList('e-carrierImplications-list', paper.carrierImplications || [], 'carrierImplications');
  renderFaqList(paper.faq || []);
  renderCitationList(paper.citations || []);
  renderSections();
}}

function setVal(id, val) {{
  const el = document.getElementById(id);
  if (el) el.value = val;
}}

// ── Title / slug auto-update ──────────────────────────────────────────────────
function onTitleChange(val) {{
  const newSlug = titleToSlug(val);
  const slugEl = document.getElementById('e-slug-display');
  const warnEl = document.getElementById('slug-warning');
  slugEl.textContent = 'slug: ' + newSlug;
  if (newSlug !== editorOrigSlug) {{
    slugEl.className = 'slug-display changed';
    warnEl.style.display = 'block';
    document.getElementById('modal-save-btn').disabled = true;
    document.getElementById('modal-publish-btn').disabled = true;
    document.getElementById('slug-confirm-check').checked = false;
  }} else {{
    slugEl.className = 'slug-display';
    warnEl.style.display = 'none';
    document.getElementById('modal-save-btn').disabled = false;
    document.getElementById('modal-publish-btn').disabled = false;
  }}
}}

function checkSlugConfirm() {{
  const checked = document.getElementById('slug-confirm-check').checked;
  document.getElementById('modal-save-btn').disabled = !checked;
  document.getElementById('modal-publish-btn').disabled = !checked;
}}

function titleToSlug(title) {{
  return title.split(':')[0].trim().toLowerCase()
    .replace(/[^a-z0-9\\s-]/g, '').trim()
    .replace(/\\s+/g, '-').replace(/-+/g, '-')
    .split('-').slice(0, 8).join('-');
}}

// ── Dynamic list rendering ────────────────────────────────────────────────────
function renderDynamicList(containerId, items, key) {{
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  items.forEach((item, i) => {{
    container.appendChild(makeListRow(key, item, i, items.length));
  }});
}}

function makeListRow(key, value, idx, total) {{
  const row = document.createElement('div');
  row.className = 'list-item';
  row.dataset.key = key;
  row.dataset.idx = idx;
  const ta = document.createElement('textarea');
  ta.className = 'field-textarea';
  ta.rows = 3;
  ta.value = value;
  ta.dataset.listKey = key;
  ta.dataset.listIdx = idx;
  const btn = document.createElement('button');
  btn.className = 'btn btn-delete btn-sm';
  btn.textContent = 'Remove';
  btn.onclick = () => removeListItem(key, idx);
  row.appendChild(ta);
  row.appendChild(btn);
  return row;
}}

function addListItem(key) {{
  const containerId = 'e-' + key + '-list';
  const container = document.getElementById(containerId);
  const items = collectList(key);
  items.push('');
  renderDynamicList(containerId, items, key);
  // Focus last textarea
  const tas = container.querySelectorAll('textarea');
  if (tas.length > 0) tas[tas.length - 1].focus();
}}

function removeListItem(key, idx) {{
  const items = collectList(key);
  items.splice(idx, 1);
  renderDynamicList('e-' + key + '-list', items, key);
}}

function collectList(key) {{
  const container = document.getElementById('e-' + key + '-list');
  if (!container) return [];
  return Array.from(container.querySelectorAll('textarea')).map(t => t.value);
}}

// ── FAQ ───────────────────────────────────────────────────────────────────────
function renderFaqList(items) {{
  const container = document.getElementById('e-faq-list');
  container.innerHTML = '';
  items.forEach((item, i) => {{
    const div = document.createElement('div');
    div.style.marginBottom = '12px';
    div.innerHTML = `
      <div style="display:flex;gap:8px;margin-bottom:6px;">
        <span style="font-size:11px;color:#8b5cf6;font-weight:700;flex:1;">Q${{i+1}}</span>
        <button class="btn btn-delete btn-sm" onclick="removeFaq(${{i}})">Remove</button>
      </div>
      <textarea class="field-textarea" rows="2" id="faq-q-${{i}}">${{escHtml(item.q || '')}}</textarea>
      <div style="font-size:11px;color:#64748b;margin:6px 0 4px;">Answer</div>
      <textarea class="field-textarea" rows="4" id="faq-a-${{i}}">${{escHtml(item.a || '')}}</textarea>
    `;
    container.appendChild(div);
  }});
}}

function addFaq() {{
  const faqs = collectFaq();
  faqs.push({{q: '', a: ''}});
  renderFaqList(faqs);
}}

function removeFaq(idx) {{
  const faqs = collectFaq();
  faqs.splice(idx, 1);
  renderFaqList(faqs);
}}

function collectFaq() {{
  const container = document.getElementById('e-faq-list');
  const result = [];
  let i = 0;
  while (document.getElementById('faq-q-' + i)) {{
    result.push({{
      q: document.getElementById('faq-q-' + i).value,
      a: document.getElementById('faq-a-' + i).value,
    }});
    i++;
  }}
  return result;
}}

// ── Citations ─────────────────────────────────────────────────────────────────
function renderCitationList(items) {{
  const container = document.getElementById('e-citations-list');
  container.innerHTML = '';
  items.forEach((item, i) => {{
    const div = document.createElement('div');
    div.style.marginBottom = '12px';
    div.innerHTML = `
      <div style="display:flex;gap:8px;margin-bottom:6px;">
        <span style="font-size:11px;color:#06b6d4;font-weight:700;flex:1;">Citation ${{i+1}}</span>
        <button class="btn btn-delete btn-sm" onclick="removeCitation(${{i}})">Remove</button>
      </div>
      <textarea class="field-textarea" rows="2" id="cit-label-${{i}}" placeholder="Label / description">${{escHtml(item.label || '')}}</textarea>
      <input type="url" class="field-input" id="cit-url-${{i}}" placeholder="https://..." value="${{escHtml(item.url || '')}}" style="margin-top:6px;">
    `;
    container.appendChild(div);
  }});
}}

function addCitation() {{
  const cits = collectCitations();
  cits.push({{label: '', url: ''}});
  renderCitationList(cits);
}}

function removeCitation(idx) {{
  const cits = collectCitations();
  cits.splice(idx, 1);
  renderCitationList(cits);
}}

function collectCitations() {{
  const result = [];
  let i = 0;
  while (document.getElementById('cit-label-' + i)) {{
    result.push({{
      label: document.getElementById('cit-label-' + i).value,
      url: document.getElementById('cit-url-' + i).value,
    }});
    i++;
  }}
  return result;
}}

// ── Sections ──────────────────────────────────────────────────────────────────
function renderSections() {{
  const container = document.getElementById('e-sections-list');
  container.innerHTML = '';
  editorSections.forEach((sec, i) => {{
    container.appendChild(makeSectionRow(sec, i));
  }});
}}

function makeSectionRow(sec, idx) {{
  const row = document.createElement('div');
  row.className = 'section-row';
  row.id = 'sec-row-' + idx;

  const typeLabel = sec.type || 'p';
  const isFirst = idx === 0;
  const isLast = idx === editorSections.length - 1;

  let contentHtml = '';
  if (['h2', 'h3', 'p', 'callout'].includes(typeLabel)) {{
    contentHtml = `<textarea class="field-textarea" rows="${{typeLabel === 'p' || typeLabel === 'callout' ? 5 : 2}}"
      id="sec-text-${{idx}}" oninput="updateSectionText(${{idx}}, this.value)">${{escHtml(sec.text || '')}}</textarea>`;
  }} else if (typeLabel === 'ul' || typeLabel === 'ol') {{
    const joined = (sec.items || []).join('\\n');
    contentHtml = `
      <div style="font-size:11px;color:#64748b;margin-bottom:4px;">One item per line</div>
      <textarea class="field-textarea" rows="6" id="sec-items-${{idx}}"
        oninput="updateSectionItems(${{idx}}, this.value)">${{escHtml(joined)}}</textarea>`;
  }} else if (typeLabel === 'table') {{
    const headers = (sec.headers || []).join('\\t');
    const rows = (sec.rows || []).map(r => r.join('\\t')).join('\\n');
    contentHtml = `
      <div style="font-size:11px;color:#64748b;margin-bottom:4px;">Headers (tab-separated)</div>
      <input type="text" class="field-input table-headers-input" id="sec-headers-${{idx}}"
        value="${{escHtml(headers)}}" oninput="updateSectionHeaders(${{idx}}, this.value)" style="margin-bottom:8px;">
      <div style="font-size:11px;color:#64748b;margin-bottom:4px;">Rows (one row per line, cells tab-separated)</div>
      <textarea class="field-textarea table-rows-input" id="sec-rows-${{idx}}"
        oninput="updateSectionRows(${{idx}}, this.value)">${{escHtml(rows)}}</textarea>
      <div style="margin-top:8px;">
        <label style="font-size:11px;color:#64748b;">Caption (optional)</label>
        <input type="text" class="field-input" id="sec-caption-${{idx}}"
          value="${{escHtml(sec.caption || '')}}" oninput="updateSectionCaption(${{idx}}, this.value)" style="margin-top:4px;">
      </div>`;
  }}

  row.innerHTML = `
    <div class="section-row-header">
      <span class="section-type-badge">${{typeLabel}}</span>
      <div class="section-row-actions">
        ${{!isFirst ? `<button class="btn btn-secondary btn-sm" onclick="moveSection(${{idx}}, -1)" title="Move up">&#x2191;</button>` : ''}}
        ${{!isLast ? `<button class="btn btn-secondary btn-sm" onclick="moveSection(${{idx}}, 1)" title="Move down">&#x2193;</button>` : ''}}
        <button class="btn btn-delete btn-sm" onclick="removeSection(${{idx}})">Remove</button>
      </div>
    </div>
    ${{contentHtml}}`;

  return row;
}}

function addSection(type) {{
  const defaults = {{
    h2: {{type:'h2', text:''}},
    h3: {{type:'h3', text:''}},
    p: {{type:'p', text:''}},
    callout: {{type:'callout', text:''}},
    ul: {{type:'ul', items:['']}},
    ol: {{type:'ol', items:['']}},
    table: {{type:'table', headers:['Column 1','Column 2'], rows:[['','']]}},
  }};
  editorSections.push(defaults[type] || {{type:'p', text:''}});
  renderSections();
  // Scroll to new section
  const last = document.getElementById('sec-row-' + (editorSections.length - 1));
  if (last) last.scrollIntoView({{behavior:'smooth', block:'nearest'}});
}}

function removeSection(idx) {{
  collectSectionData();  // persist current edits before splice
  editorSections.splice(idx, 1);
  renderSections();
}}

function moveSection(idx, dir) {{
  collectSectionData();
  const target = idx + dir;
  if (target < 0 || target >= editorSections.length) return;
  [editorSections[idx], editorSections[target]] = [editorSections[target], editorSections[idx]];
  renderSections();
}}

function updateSectionText(idx, val) {{ editorSections[idx].text = val; }}
function updateSectionItems(idx, val) {{ editorSections[idx].items = val.split('\\n'); }}
function updateSectionHeaders(idx, val) {{ editorSections[idx].headers = val.split('\\t'); }}
function updateSectionRows(idx, val) {{
  editorSections[idx].rows = val.split('\\n').map(r => r.split('\\t'));
}}
function updateSectionCaption(idx, val) {{ editorSections[idx].caption = val; }}

function collectSectionData() {{
  // Sync DOM → editorSections for all visible section inputs
  editorSections.forEach((sec, idx) => {{
    const typeLabel = sec.type;
    if (['h2','h3','p','callout'].includes(typeLabel)) {{
      const el = document.getElementById('sec-text-' + idx);
      if (el) sec.text = el.value;
    }} else if (typeLabel === 'ul' || typeLabel === 'ol') {{
      const el = document.getElementById('sec-items-' + idx);
      if (el) sec.items = el.value.split('\\n');
    }} else if (typeLabel === 'table') {{
      const hEl = document.getElementById('sec-headers-' + idx);
      if (hEl) sec.headers = hEl.value.split('\\t');
      const rEl = document.getElementById('sec-rows-' + idx);
      if (rEl) sec.rows = rEl.value.split('\\n').map(r => r.split('\\t'));
      const cEl = document.getElementById('sec-caption-' + idx);
      if (cEl) sec.caption = cEl.value;
    }}
  }});
}}

// ── Collect full paper from form ──────────────────────────────────────────────
function collectForm() {{
  collectSectionData();
  return {{
    slug: titleToSlug(document.getElementById('e-title').value) !== editorOrigSlug
      ? titleToSlug(document.getElementById('e-title').value)
      : editorOrigSlug,
    title: document.getElementById('e-title').value,
    subtitle: document.getElementById('e-subtitle').value,
    category: document.getElementById('e-category').value,
    audience: document.getElementById('e-audience').value,
    region: document.getElementById('e-region').value,
    readTime: document.getElementById('e-readTime').value,
    published: document.getElementById('e-published').value,
    abstract: document.getElementById('e-abstract').value,
    keyFindings: collectList('keyFindings'),
    shopImplications: collectList('shopImplications'),
    carrierImplications: collectList('carrierImplications'),
    sections: editorSections,
    faq: collectFaq(),
    citations: collectCitations(),
  }};
}}

// ── Save article ──────────────────────────────────────────────────────────────
async function saveArticle() {{
  const paper = collectForm();
  const newSlug = paper.slug;
  const btn = document.getElementById('modal-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  clearEditorBanner();

  try {{
    // If slug changed, rename first
    if (newSlug !== editorOrigSlug) {{
      const renameResp = await authedFetch('/api/article/' + editorOrigSlug + '/rename', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{new_title: paper.title}}),
      }});
      const renameData = await renameResp.json();
      if (!renameData.success) {{
        showEditorBanner('error', 'Rename failed: ' + renameData.message);
        btn.disabled = false; btn.textContent = 'Save Draft'; return;
      }}
      editorSlug = renameData.new_slug;
      editorOrigSlug = renameData.new_slug;
      paper.slug = renameData.new_slug;
      document.getElementById('slug-warning').style.display = 'none';
      document.getElementById('e-slug-display').textContent = 'slug: ' + renameData.new_slug;
      document.getElementById('e-slug-display').className = 'slug-display';
    }}

    // Save the edited paper
    const resp = await authedFetch('/api/article/' + editorSlug + '/edit', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{paper}}),
    }});
    const data = await resp.json();
    if (data.success) {{
      showEditorBanner('success', 'Article saved successfully.');
      // Refresh the draft card title if on drafts tab
      refreshDraftCard(editorSlug, paper.title, paper.abstract ? paper.abstract.substring(0, 200) + '...' : '');
    }} else {{
      showEditorBanner('error', 'Save failed: ' + data.message);
    }}
  }} catch(e) {{
    showEditorBanner('error', 'Network error: ' + e.message);
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'Save Draft';
  }}
}}

async function saveAndPublish() {{
  await saveArticle();
  // Check if banner shows error before publishing
  const banner = document.getElementById('editor-banner');
  if (banner.innerHTML.includes('error-banner')) return;
  await editorPublish();
}}

async function editorPublish() {{
  const btn = document.getElementById('modal-publish-btn');
  btn.disabled = true;
  btn.textContent = 'Publishing...';
  try {{
    const resp = await authedFetch('/publish/' + editorSlug, {{method: 'POST'}});
    const data = await resp.json();
    if (data.success) {{
      showEditorBanner('success', '&#x2705; Published! ' + (data.url ? '<a href="' + data.url + '" target="_blank" style="color:#4ade80">' + data.url + '</a>' : ''));
      btn.textContent = '&#x2705; Published';
      btn.style.background = '#1e4d2b';
      btn.style.color = '#4ade80';
    }} else {{
      showEditorBanner('error', 'Publish failed: ' + data.message);
      btn.disabled = false; btn.textContent = 'Save & Publish';
    }}
  }} catch(e) {{
    showEditorBanner('error', 'Network error: ' + e.message);
    btn.disabled = false; btn.textContent = 'Save & Publish';
  }}
}}

function refreshDraftCard(slug, newTitle, newAbstract) {{
  const card = document.getElementById('card-' + slug);
  if (card) {{
    const h2 = card.querySelector('h2');
    if (h2) h2.textContent = newTitle;
    const abs = card.querySelector('.abstract');
    if (abs && newAbstract) abs.textContent = newAbstract;
  }}
}}

// ── LinkedIn editor ───────────────────────────────────────────────────────────
function populateLinkedinEditor(posts) {{
  setVal('li-hook', posts.hook || '');
  setVal('li-insight', posts.insight || '');
  setVal('li-story', posts.story || '');
}}

async function regenLinkedin() {{
  const btn = document.getElementById('regen-li-btn');
  btn.disabled = true;
  btn.textContent = 'Regenerating...';
  try {{
    const resp = await authedFetch('/api/article/' + editorSlug + '/regenerate-linkedin', {{method: 'POST'}});
    const data = await resp.json();
    if (data.success) {{
      populateLinkedinEditor(data.posts);
      showEditorBanner('success', 'LinkedIn posts regenerated.');
    }} else {{
      showEditorBanner('error', 'Regeneration failed: ' + data.message);
    }}
  }} catch(e) {{
    showEditorBanner('error', 'Network error: ' + e.message);
  }} finally {{
    btn.disabled = false;
    btn.textContent = '&#x21BA; Regenerate All';
  }}
}}

// ── Live Site tab ─────────────────────────────────────────────────────────────
async function loadLiveSite() {{
  liveLoaded = true;
  const container = document.getElementById('live-articles');
  container.innerHTML = '<div class="empty"><h2><span class="spinner"></span> Loading...</h2></div>';

  try {{
    // Check cache state first
    const syncResp = await authedFetch('/api/sync');
    const syncData = await syncResp.json();
    updateSyncBar(syncData);

    const resp = await authedFetch('/api/site-articles');
    const data = await resp.json();
    renderLiveArticles(data.articles || []);
  }} catch(e) {{
    container.innerHTML = '<div class="error-banner">Failed to load live articles: ' + e.message + '</div>';
  }}
}}

async function runSync() {{
  liveLoaded = false;
  const btn = document.getElementById('sync-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  const container = document.getElementById('live-articles');
  container.innerHTML = '<div class="empty"><h2><span class="spinner"></span> Syncing from GitHub...</h2></div>';

  try {{
    const resp = await authedFetch('/api/sync?force=true');
    const data = await resp.json();
    updateSyncBar(data);

    const artResp = await authedFetch('/api/site-articles');
    const artData = await artResp.json();
    renderLiveArticles(artData.articles || []);
    liveLoaded = true;
  }} catch(e) {{
    container.innerHTML = '<div class="error-banner">Sync failed: ' + e.message + '</div>';
  }} finally {{
    btn.disabled = false;
    btn.innerHTML = '&#x21BB; Sync Now';
  }}
}}

function updateSyncBar(data) {{
  const dot = document.getElementById('sync-dot');
  const timeEl = document.getElementById('sync-time');
  const count = document.getElementById('tc-live');

  if (data.last_sync) {{
    const dt = new Date(data.last_sync);
    timeEl.textContent = 'Last synced: ' + dt.toLocaleString() + (data.was_cached ? ' (cached)' : '');
    dot.className = 'sync-dot';
  }} else {{
    timeEl.textContent = 'Never synced';
    dot.className = 'sync-dot never';
  }}

  if (data.synced_count !== undefined) {{
    count.textContent = data.synced_count;
    count.className = data.synced_count > 0 ? 'tab-count has-items' : 'tab-count';
  }}
}}

function renderLiveArticles(articles) {{
  const container = document.getElementById('live-articles');
  if (!articles.length) {{
    container.innerHTML = '<div class="empty"><h2>No articles found on live site</h2><p>Try syncing from GitHub.</p></div>';
    return;
  }}
  container.innerHTML = articles.map(a => `
    <div class="card">
      <div class="icons">
        <span class="badge badge-blue">${{escHtml(a.region || 'Canada')}}</span>
        ${{a.published_date ? '<span class="badge badge-gray">' + escHtml(a.published_date) + '</span>' : ''}}
      </div>
      <h2>${{escHtml(a.title)}}</h2>
      <div class="meta">${{escHtml(a.slug)}} &middot; ${{escHtml(a.read_time || '')}}</div>
      <div class="abstract">${{escHtml(a.abstract)}}</div>
      <div class="actions">
        <a href="${{escHtml(a.site_url)}}" target="_blank" class="btn btn-secondary">View Live &rarr;</a>
        <button onclick="pullForEdit('${{a.slug}}')" class="btn btn-edit">&#x2B07; Pull for Edit</button>
        <button onclick="deleteLiveArticle('${{a.slug}}', this)" class="btn btn-delete btn-sm" title="Remove from live website">&#x1F5D1; Remove from Site</button>
      </div>
      <div id="pull-result-${{a.slug}}" style="margin-top:8px;"></div>
    </div>`).join('');
}}

async function pullForEdit(slug, overwrite) {{
  const resultEl = document.getElementById('pull-result-' + slug);
  if (resultEl) resultEl.innerHTML = '<span class="spinner"></span>';

  const url = '/api/sync/pull/' + slug + (overwrite ? '?overwrite=true' : '');
  try {{
    const resp = await authedFetch(url, {{method: 'POST'}});
    const data = await resp.json();
    if (data.success) {{
      if (resultEl) resultEl.innerHTML = '<div class="success-banner">Pulled to drafts! Switch to the Drafts tab to edit.</div>';
      // Switch to drafts tab after short delay
      setTimeout(() => switchTab('drafts', document.querySelector('.tab-btn')), 1500);
    }} else if (data.would_overwrite) {{
      if (confirm('"' + slug + '" already exists in your drafts. Overwrite it with the live version?')) {{
        pullForEdit(slug, true);
      }} else {{
        if (resultEl) resultEl.innerHTML = '';
      }}
    }} else {{
      if (resultEl) resultEl.innerHTML = '<div class="error-banner">' + escHtml(data.message) + '</div>';
    }}
  }} catch(e) {{
    if (resultEl) resultEl.innerHTML = '<div class="error-banner">Error: ' + e.message + '</div>';
  }}
}}

// ── LinkedIn Hub tab ──────────────────────────────────────────────────────────
async function loadLinkedin() {{
  linkedinLoaded = true;
  const container = document.getElementById('linkedin-list');
  container.innerHTML = '<div class="empty"><h2><span class="spinner"></span> Loading...</h2></div>';

  try {{
    const resp = await authedFetch('/api/linkedin-hub');
    const data = await resp.json();
    renderLinkedinHub(data.articles || []);
  }} catch(e) {{
    container.innerHTML = '<div class="error-banner">Failed to load LinkedIn posts: ' + e.message + '</div>';
  }}
}}

function renderLinkedinHub(articles) {{
  const container = document.getElementById('linkedin-list');
  const countEl = document.getElementById('li-count');
  if (!articles.length) {{
    container.innerHTML = '<div class="empty"><h2>No LinkedIn posts yet</h2><p>Generate articles first.</p></div>';
    countEl.textContent = '';
    return;
  }}
  countEl.textContent = articles.length + ' article(s)';
  container.innerHTML = articles.map((a, i) => `
    <div class="li-accordion" data-title="${{escHtml(a.title)}}">
      <div class="li-accordion-header" onclick="toggleAccordion(this)">
        <span>${{escHtml(a.title)}}</span>
        <span style="color:#64748b;font-size:12px;">${{a.has_all ? '3 variants' : 'partial'}} &#x25BC;</span>
      </div>
      <div class="li-accordion-body">
        ${{renderLinkedinVariant('Hook (Myles)', a.posts.hook || '', a.slug, 'hook')}}
        ${{renderLinkedinVariant('Insight (Ali)', a.posts.insight || '', a.slug, 'insight')}}
        ${{renderLinkedinVariant('Story (Myles)', a.posts.story || '', a.slug, 'story')}}
        <div style="padding:10px 16px;border-top:1px solid #1e293b;">
          <button onclick="openEditor('${{a.slug}}', 'local')" class="btn btn-edit btn-sm">&#x270F; Edit Article</button>
        </div>
      </div>
    </div>`).join('');
}}

function renderLinkedinVariant(label, text, slug, variant) {{
  if (!text) return '';
  return `
    <div class="li-variant">
      <div class="li-variant-label">
        <span>${{label}}</span>
        <button class="btn btn-copy" onclick="copyText('li-text-${{slug}}-${{variant}}')">Copy</button>
      </div>
      <div class="linkedin-block" id="li-text-${{slug}}-${{variant}}">${{escHtml(text)}}</div>
    </div>`;
}}

function toggleAccordion(header) {{
  const body = header.nextElementSibling;
  const isOpen = body.style.display === 'block';
  body.style.display = isOpen ? 'none' : 'block';
  const arrow = header.querySelector('span:last-child');
  if (arrow) arrow.innerHTML = (isOpen ? 'partial &#x25BC;' : header.parentElement.dataset.title + ' &#x25B2;').replace(/^.+ /, '') || (isOpen ? '&#x25BC;' : '&#x25B2;');
}}

function filterLinkedin(query) {{
  const q = query.toLowerCase();
  document.querySelectorAll('.li-accordion').forEach(el => {{
    const title = (el.dataset.title || '').toLowerCase();
    el.style.display = title.includes(q) ? 'block' : 'none';
  }});
}}

function copyText(id) {{
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.textContent;
  navigator.clipboard.writeText(text).then(() => {{
    // Brief flash
    el.style.borderColor = '#16a34a';
    setTimeout(() => el.style.borderColor = '', 1000);
  }});
}}

// ── Analytics tab ─────────────────────────────────────────────────────────────
async function loadAnalytics() {{
  analyticsLoaded = true;
  const container = document.getElementById('analytics-content');
  container.innerHTML = '<div class="empty"><h2><span class="spinner"></span> Loading...</h2></div>';
  try {{
    const resp = await authedFetch('/api/stats');
    const data = await resp.json();
    renderAnalytics(data);
  }} catch(e) {{
    container.innerHTML = '<div class="error-banner">Failed to load stats: ' + e.message + '</div>';
  }}
}}

function renderAnalytics(data) {{
  const container = document.getElementById('analytics-content');
  const lr = data.last_run || {{}};
  const history = data.published_history || [];

  const histRows = history.map(entry => `
    <tr>
      <td><a href="{SITE_URL}/research/${{escHtml(entry.slug)}}" target="_blank">${{escHtml(entry.slug)}}</a></td>
      <td>${{escHtml(entry.published_at ? new Date(entry.published_at).toLocaleString() : 'Unknown')}}</td>
    </tr>`).join('');

  container.innerHTML = `
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-num">${{data.published_count || 0}}</div>
        <div class="stat-label">Published Articles</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">${{data.pending_count || 0}}</div>
        <div class="stat-label">Pending Drafts</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">${{lr.articles_succeeded || '—'}}</div>
        <div class="stat-label">Last Run Articles</div>
      </div>
      <div class="stat-card">
        <div class="stat-num" style="font-size:22px;">${{lr.duration_seconds ? Math.round(lr.duration_seconds) + 's' : '—'}}</div>
        <div class="stat-label">Last Run Duration</div>
      </div>
    </div>
    ${{lr.timestamp ? '<div class="info-banner">Last pipeline run: ' + new Date(lr.timestamp).toLocaleString() + ' &middot; ' + (lr.errors && lr.errors.length ? lr.errors.length + ' error(s)' : 'No errors') + '</div>' : ''}}
    <h3 style="font-size:14px;color:#94a3b8;margin-bottom:12px;">Publication History</h3>
    ${{history.length ? `
      <div class="card" style="padding:0;overflow:hidden;">
        <table class="data-table">
          <thead><tr><th>Slug</th><th>Published At</th></tr></thead>
          <tbody>${{histRows}}</tbody>
        </table>
      </div>` : '<div class="info-banner">No articles published yet.</div>'}}`;
}}

// ── Pipeline (Run Pipeline tab) ───────────────────────────────────────────────
let pipelineES = null;

function forceRun(btn) {{
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add('running');
  const direction = (document.getElementById('direction-input') || {{}}).value || '';
  btn.textContent = direction ? '&#x23F3; Running (directed)...' : '&#x23F3; Running...';

  const body = document.getElementById('console-body');
  const status = document.getElementById('console-status');
  body.innerHTML = '';
  status.className = 'console-status running';
  status.textContent = 'Running';

  if (direction) appendLog('Direction: ' + direction);

  fetch('/run', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{direction}}),
  }})
    .then(r => r.json())
    .then(d => {{
      if (!d.started && d.message && d.message.includes('already')) appendLog('Already running — connecting to stream...');
      startStream(btn);
    }})
    .catch(e => {{ appendLog('ERROR: ' + e.message); setDone(btn, false); }});
}}

function startStream(btn) {{
  if (pipelineES) pipelineES.close();
  pipelineES = new EventSource('/run/stream');
  pipelineES.onmessage = e => {{
    if (e.data === '__DONE__') {{
      pipelineES.close();
      setDone(btn, true);
      setTimeout(() => location.reload(), 3000);
      return;
    }}
    appendLog(e.data);
  }};
  pipelineES.onerror = () => {{ pipelineES.close(); appendLog('Stream disconnected.'); setDone(btn, false); }};
}}

function appendLog(line) {{
  const body = document.getElementById('console-body');
  const div = document.createElement('div');
  div.className = 'log-line';
  let cls = 'log-text';
  if (line.includes('ERROR') || line.includes('error')) cls += ' err';
  else if (line.includes('STEP ') || line.includes('===')) cls += ' step';
  else if (line.includes('Done') || line.includes('complete') || line.includes('\\u2713')) cls += ' done';
  div.innerHTML = '<span class="' + cls + '">' + escHtml(line) + '</span>';
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}}

function setDone(btn, success) {{
  const status = document.getElementById('console-status');
  status.className = 'console-status ' + (success ? 'done' : 'error');
  status.textContent = success ? 'Done' : 'Error';
  btn.disabled = false;
  btn.textContent = '&#x26A1; Force Run Pipeline';
  btn.classList.remove('running');
  if (success) appendLog('\\u2713 Complete — reloading in 3 seconds...');
}}

// ── Publish / delete (draft cards) ───────────────────────────────────────────
async function publishArticle(slug, btn) {{
  btn.disabled = true;
  btn.textContent = 'Publishing...';
  const resultDiv = document.getElementById('result-' + slug);
  try {{
    const resp = await authedFetch('/publish/' + slug, {{method: 'POST'}});
    const data = await resp.json();
    if (data.success) {{
      resultDiv.innerHTML = '<div class="success-banner">&#x2705; ' + escHtml(data.message) +
        (data.url ? ' <a href="' + escHtml(data.url) + '" target="_blank" style="color:#4ade80">' + escHtml(data.url) + '</a>' : '') + '</div>';
      btn.textContent = '&#x2705; Published';
      btn.style.background = '#1e4d2b';
      btn.style.color = '#4ade80';
      btn.style.cursor = 'not-allowed';
      btn.onclick = null;
    }} else {{
      resultDiv.innerHTML = '<div class="error-banner">&#x274C; ' + escHtml(data.message) + '</div>';
      btn.disabled = false; btn.textContent = '&#x1F680; Publish';
    }}
  }} catch(e) {{
    resultDiv.innerHTML = '<div class="error-banner">&#x274C; ' + e.message + '</div>';
    btn.disabled = false; btn.textContent = '&#x1F680; Publish';
  }}
}}

async function deleteArticle(slug, btn, isPublished) {{
  let removeFromGithub = false;
  if (isPublished) {{
    const choice = confirm(
      'DELETE "' + slug + '"\n\n' +
      'This article is LIVE on the website.\n\n' +
      'Click OK to delete from BOTH the dashboard AND the live site (removes from rocketpros.app and triggers a Vercel redeploy).\n\n' +
      'Click Cancel to abort.'
    );
    if (!choice) return;
    removeFromGithub = true;
  }} else {{
    if (!confirm('Delete "' + slug + '" from drafts? This removes local files only.')) return;
  }}
  btn.disabled = true; btn.textContent = 'Deleting...';
  try {{
    const url = '/article/' + slug + (removeFromGithub ? '?remove_from_github=true' : '');
    const resp = await authedFetch(url, {{method: 'DELETE'}});
    const data = await resp.json();
    if (data.success) {{
      const card = document.getElementById('card-' + slug);
      if (card) {{ card.style.opacity = '0'; card.style.transition = 'opacity .3s'; setTimeout(() => card.remove(), 300); }}
      if (removeFromGithub) {{
        const gh = data.github || {{}};
        alert('Deleted from dashboard and GitHub.\n' + (gh.message || 'Vercel redeploy triggered — page will go offline in ~60 seconds.'));
      }}
    }} else {{ alert('Delete failed: ' + data.message); btn.disabled = false; btn.textContent = '&#x1F5D1; Delete'; }}
  }} catch(e) {{ alert('Error: ' + e.message); btn.disabled = false; btn.textContent = '&#x1F5D1; Delete'; }}
}}

async function deleteLiveArticle(slug, btn) {{
  const confirmed = confirm(
    'REMOVE "' + slug + '" FROM LIVE SITE\n\n' +
    'This will:\n' +
    '  - Delete the article TypeScript file from GitHub\n' +
    '  - Delete the hero image from GitHub\n' +
    '  - Remove it from the research index\n' +
    '  - Trigger a Vercel redeploy (page offline in ~60s)\n\n' +
    'This cannot be undone without re-publishing. Continue?'
  );
  if (!confirmed) return;
  btn.disabled = true; btn.textContent = 'Removing...';
  try {{
    const resp = await authedFetch('/article/' + slug + '?remove_from_github=true', {{method: 'DELETE'}});
    const data = await resp.json();
    if (data.success) {{
      const card = btn.closest('.card');
      if (card) {{ card.style.opacity = '0'; card.style.transition = 'opacity .3s'; setTimeout(() => card.remove(), 300); }}
      alert('Removed from live site. Vercel is redeploying — the page will go offline in ~60 seconds.');
    }} else {{
      alert('Remove failed: ' + data.message);
      btn.disabled = false; btn.textContent = '&#x1F5D1; Remove from Site';
    }}
  }} catch(e) {{ alert('Error: ' + e.message); btn.disabled = false; btn.textContent = '&#x1F5D1; Remove from Site'; }}
}}

// ── Admin: User Management ────────────────────────────────────────────────────
let adminLoaded = false;

async function loadUsers() {{
  const container = document.getElementById('users-list');
  if (!container) return;
  try {{
    const resp = await fetch('/api/users');
    const data = await resp.json();
    renderUsers(data.users || []);
    adminLoaded = true;
  }} catch(e) {{
    container.innerHTML = '<div class="error-banner">Failed to load users: ' + e.message + '</div>';
  }}
}}

function renderUsers(users) {{
  const container = document.getElementById('users-list');
  if (!users.length) {{
    container.innerHTML = '<div class="info-banner">No users found.</div>';
    return;
  }}
  container.innerHTML = `
    <table class="data-table">
      <thead><tr><th>Username</th><th>Role</th><th>Created</th><th>Actions</th></tr></thead>
      <tbody>
        ${{users.map(u => `
          <tr id="user-row-${{escHtml(u.username)}}">
            <td style="font-weight:600;">${{escHtml(u.username)}}</td>
            <td>
              <select onchange="changeRole('${{escHtml(u.username)}}', this.value)" style="background:#1e293b;color:#e2e8f0;border:1px solid #2d3748;padding:4px 8px;border-radius:4px;">
                <option value="viewer" ${{u.role === 'viewer' ? 'selected' : ''}}>Viewer</option>
                <option value="admin" ${{u.role === 'admin' ? 'selected' : ''}}>Admin</option>
              </select>
            </td>
            <td style="font-size:12px;color:#64748b;">${{u.created ? new Date(u.created).toLocaleDateString() : ''}}</td>
            <td style="display:flex;gap:6px;flex-wrap:wrap;">
              <button class="btn btn-secondary btn-sm" onclick="promptChangePassword('${{escHtml(u.username)}}')" title="Change password">&#x1F511; Password</button>
              <button class="btn btn-delete btn-sm" onclick="removeUser('${{escHtml(u.username)}}')" title="Delete user">&#x1F5D1;</button>
            </td>
          </tr>`).join('')}}
      </tbody>
    </table>`;
}}

async function addUser() {{
  const username = document.getElementById('new-username').value.trim();
  const password = document.getElementById('new-password').value;
  const role = document.getElementById('new-role').value;
  const resultEl = document.getElementById('user-add-result');
  if (!username || !password) {{ resultEl.innerHTML = '<div class="error-banner">Username and password required.</div>'; return; }}
  try {{
    const resp = await fetch('/api/users', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{username, password, role}}),
    }});
    const data = await resp.json();
    if (data.success) {{
      resultEl.innerHTML = '<div class="success-banner">' + escHtml(data.message) + '</div>';
      document.getElementById('new-username').value = '';
      document.getElementById('new-password').value = '';
      loadUsers();
    }} else {{
      resultEl.innerHTML = '<div class="error-banner">' + escHtml(data.message) + '</div>';
    }}
  }} catch(e) {{ resultEl.innerHTML = '<div class="error-banner">Error: ' + e.message + '</div>'; }}
}}

async function removeUser(username) {{
  if (!confirm('Delete user "' + username + '"? They will no longer be able to log in.')) return;
  try {{
    const resp = await fetch('/api/users/' + encodeURIComponent(username), {{method: 'DELETE'}});
    const data = await resp.json();
    if (data.success) {{
      const row = document.getElementById('user-row-' + username);
      if (row) row.remove();
    }} else {{
      alert(data.message);
    }}
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

async function changeRole(username, role) {{
  try {{
    const resp = await fetch('/api/users/' + encodeURIComponent(username) + '/role', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{role}}),
    }});
    const data = await resp.json();
    if (!data.success) alert(data.message);
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

async function promptChangePassword(username) {{
  const newPw = prompt('New password for "' + username + '":');
  if (!newPw) return;
  try {{
    const resp = await fetch('/api/users/' + encodeURIComponent(username) + '/password', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{password: newPw}}),
    }});
    const data = await resp.json();
    alert(data.message);
  }} catch(e) {{ alert('Error: ' + e.message); }}
}}

// ── Utilities ─────────────────────────────────────────────────────────────────
function escHtml(t) {{
  if (!t) return '';
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function showEditorBanner(type, msg) {{
  const el = document.getElementById('editor-banner');
  el.innerHTML = '<div class="' + type + '-banner">' + msg + '</div>';
  el.scrollIntoView({{behavior:'smooth', block:'nearest'}});
}}

function clearEditorBanner() {{
  document.getElementById('editor-banner').innerHTML = '';
}}

// Authenticated fetch — session cookie is sent automatically on same-origin requests.
function authedFetch(url, opts) {{
  return fetch(url, opts || {{}});
}}

// ── Auto-connect if pipeline already running on page load ────────────────────
fetch('/run/status')
  .then(r => r.json())
  .then(d => {{
    if (d.running) {{
      const btn = document.getElementById('run-btn');
      if (btn) {{
        btn.disabled = true; btn.textContent = '&#x23F3; Running...'; btn.classList.add('running');
        document.getElementById('console-status').className = 'console-status running';
        document.getElementById('console-status').textContent = 'Running';
        startStream(btn);
        // Switch to pipeline tab automatically
        switchTab('pipeline', document.querySelectorAll('.tab-btn')[4]);
      }}
    }}
  }})
  .catch(() => {{}});  // ignore — user may not be logged in yet
</script>
</body>
</html>"""


# ── Login / logout ─────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RocketPros — Sign In</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f1117; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .login-box { background: #1e293b; border: 1px solid #2d3748; border-radius: 12px;
                  padding: 40px 36px; width: 100%; max-width: 380px; }
    h1 { color: #06b6d4; font-size: 20px; font-weight: 700; margin-bottom: 6px; }
    p { color: #64748b; font-size: 13px; margin-bottom: 28px; }
    label { display: block; font-size: 12px; font-weight: 600; color: #94a3b8;
             text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
    input { width: 100%; background: #0f1117; border: 1px solid #2d3748; border-radius: 6px;
             color: #e2e8f0; padding: 10px 12px; font-size: 14px; margin-bottom: 16px; }
    input:focus { outline: none; border-color: #06b6d4; }
    button { width: 100%; background: #06b6d4; color: #0f1117; border: none; border-radius: 6px;
              padding: 11px; font-size: 15px; font-weight: 700; cursor: pointer; margin-top: 4px; }
    button:hover { background: #0891b2; }
    .error { background: #3b1818; border: 1px solid #ef4444; border-radius: 6px;
              padding: 10px 14px; color: #fca5a5; font-size: 13px; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="login-box">
    <h1>&#x1F680; RocketPros</h1>
    <p>Marketing Super Hub — sign in to continue</p>
    __ERROR_BLOCK__
    <form method="post" action="/login">
      <label for="username">Username</label>
      <input type="text" name="username" id="username" autocomplete="username" required autofocus>
      <label for="password">Password</label>
      <input type="password" name="password" id="password" autocomplete="current-password" required>
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    error_block = f'<div class="error">{_esc(error)}</div>' if error else ""
    return _LOGIN_HTML.replace("__ERROR_BLOCK__", error_block)


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = verify_user(username.strip(), password, OUTPUT_DIR)
    if not user:
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)
    request.session["user"] = user
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── User management API (admin only) ───────────────────────────────────────────

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class ChangePasswordRequest(BaseModel):
    password: str


class ChangeRoleRequest(BaseModel):
    role: str


@app.get("/api/users")
def api_list_users(_: dict = Depends(require_admin)):
    return JSONResponse({"users": list_users(OUTPUT_DIR)})


@app.post("/api/users")
def api_create_user(body: CreateUserRequest, _: dict = Depends(require_admin)):
    result = create_user(body.username.strip(), body.password, body.role, OUTPUT_DIR)
    return JSONResponse(result)


@app.delete("/api/users/{username}")
def api_delete_user(username: str, _: dict = Depends(require_admin)):
    result = delete_user(username, OUTPUT_DIR)
    return JSONResponse(result)


@app.post("/api/users/{username}/password")
def api_change_password(username: str, body: ChangePasswordRequest, _: dict = Depends(require_admin)):
    result = change_password(username, body.password, OUTPUT_DIR)
    return JSONResponse(result)


@app.post("/api/users/{username}/role")
def api_change_role(username: str, body: ChangeRoleRequest, _: dict = Depends(require_admin)):
    result = change_role(username, body.role, OUTPUT_DIR)
    return JSONResponse(result)


# ── Health / pipeline routes ───────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "running": run_state.running}


@app.get("/run/status")
def run_status(_: dict = Depends(require_auth)):
    return JSONResponse({"running": run_state.running, "log_count": len(run_state.logs)})


@app.post("/run")
def force_run(body: RunRequest = RunRequest(), _: dict = Depends(require_auth)):
    if run_state.running:
        return JSONResponse({"started": False, "message": "Pipeline is already running"})
    run_state.start()
    t = threading.Thread(target=_run_pipeline_thread, kwargs={"direction": body.direction.strip()}, daemon=True)
    t.start()
    return JSONResponse({"started": True, "message": "Pipeline started", "direction": body.direction.strip()})


@app.get("/run/stream")
async def run_stream(_: dict = Depends(require_auth)):
    import asyncio

    async def event_generator():
        idx = 0
        while True:
            new_lines, still_running = run_state.snapshot(idx)
            for line in new_lines:
                yield f"data: {line.replace(chr(10), ' ')}\n\n"
                idx += 1
            if not still_running and idx >= len(run_state.logs):
                yield "data: __DONE__\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Main dashboard ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: dict = Depends(require_auth)):
    articles = get_pending_articles()
    user = request.session.get("user", {})
    return _build_dashboard_html(articles, current_user=user)


# ── Legacy article detail (redirects to editor modal via JS) ──────────────────

@app.get("/article/{slug}", response_class=HTMLResponse)
def article_detail(slug: str, _: dict = Depends(require_auth)):
    """Legacy detail page — renders minimal page that auto-opens the editor modal."""
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
            linkedin_html += f'<div class="linkedin-block">{_esc(post)}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(article['title'])}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <a href="/" class="back">&larr; Back to dashboard</a>
      <h1 style="margin-top:8px">{_esc(article['title'])}</h1>
      <p>{_esc(slug)} &middot; {_esc(article['read_time'])}</p>
    </div>
  </div>
  <div class="actions" style="margin-bottom:24px;">
    <button onclick="window.location='/'" class="btn btn-secondary">&larr; Dashboard</button>
    <button onclick="publishArticle('{_esc(slug)}', this)" class="btn btn-publish">&#x1F680; Publish to Website</button>
    <a href="{SITE_URL}/research/{_esc(slug)}" target="_blank" class="btn btn-secondary">Preview Live URL &rarr;</a>
    {'<a href="/image/' + slug + '" download="' + slug + '.png" class="btn btn-secondary">&#x2B07; Download Image</a>' if article["has_image"] else ''}
  </div>
  <div id="result" style="margin-bottom:16px;"></div>
  {'<img src="/image/' + slug + '" class="img-thumb" alt="Hero">' if article["has_image"] else ''}
  {'<div class="section-label">LinkedIn Posts</div>' + linkedin_html if linkedin_html else ''}
  <div class="section-label">TypeScript — /lib/research/papers/{_esc(slug)}.ts</div>
  <pre>{_esc(ts_code)}</pre>
</div>
<script>
async function publishArticle(slug, btn) {{
  btn.disabled = true; btn.textContent = 'Publishing...';
  const resultDiv = document.getElementById('result');
  try {{
    const resp = await fetch('/publish/' + slug, {{method: 'POST'}});
    const data = await resp.json();
    if (data.success) {{
      resultDiv.innerHTML = '<div class="success-banner">&#x2705; ' + data.message + (data.url ? ' <a href="' + data.url + '" target="_blank" style="color:#4ade80">' + data.url + '</a>' : '') + '</div>';
      btn.textContent = '&#x2705; Published'; btn.style.background = '#374151';
    }} else {{
      resultDiv.innerHTML = '<div class="error-banner">&#x274C; ' + data.message + '</div>';
      btn.disabled = false; btn.textContent = '&#x1F680; Publish to Website';
    }}
  }} catch(e) {{
    resultDiv.innerHTML = '<div class="error-banner">&#x274C; ' + e.message + '</div>';
    btn.disabled = false; btn.textContent = '&#x1F680; Publish to Website';
  }}
}}
</script>
</body>
</html>"""


# ── Image download ─────────────────────────────────────────────────────────────

@app.get("/image/{slug}")
def download_image(slug: str, _: dict = Depends(require_auth)):
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


# ── Publish / delete ───────────────────────────────────────────────────────────

@app.post("/publish/{slug}")
def publish(slug: str, _: dict = Depends(require_auth)):
    result = publish_article(slug)
    return JSONResponse(content=result)


@app.delete("/article/{slug}")
def remove_article(slug: str, remove_from_github: bool = False, _: dict = Depends(require_auth)):
    result = delete_article(slug, remove_from_github=remove_from_github)
    return JSONResponse(content=result)


# ── NEW: Sync API ──────────────────────────────────────────────────────────────

@app.get("/api/sync")
def api_sync(force: bool = False, _: dict = Depends(require_auth)):
    """Trigger a GitHub sync and return results (respects TTL unless force=True)."""
    from modules.site_syncer import sync_live_articles
    result = sync_live_articles(OUTPUT_DIR, force=force)
    return JSONResponse(result)


@app.get("/api/site-articles")
def api_site_articles(_: dict = Depends(require_auth)):
    """Return cached live site articles as JSON for the Live Site tab."""
    from modules.site_syncer import get_live_articles
    articles = get_live_articles(OUTPUT_DIR)
    return JSONResponse({"articles": articles})


@app.post("/api/sync/pull/{slug}")
def api_sync_pull(slug: str, overwrite: bool = False, _: dict = Depends(require_auth)):
    """Copy a live article from the GitHub cache into output/articles/ for editing."""
    from modules.site_syncer import pull_live_article_for_edit
    result = pull_live_article_for_edit(slug, OUTPUT_DIR, overwrite=overwrite)
    return JSONResponse(result)


# ── NEW: Article preview / edit / rename ──────────────────────────────────────

@app.get("/api/article/{slug}/preview")
def api_article_preview(slug: str, source: str = "local", _: dict = Depends(require_auth)):
    """
    Return a parsed paper dict for the editor modal.
    source='local'  → reads from output/articles/{slug}.ts
    source='live'   → reads from output/site_cache/{slug}.ts
    """
    from modules.article_editor import parse_ts_to_dict

    if source == "live":
        ts_path = OUTPUT_DIR / "site_cache" / f"{slug}.ts"
    else:
        ts_path = OUTPUT_DIR / "articles" / f"{slug}.ts"

    if not ts_path.exists():
        return JSONResponse({"success": False, "message": f"File not found: {ts_path}", "paper": {}, "linkedin_posts": {}})

    try:
        ts_code = ts_path.read_text(encoding="utf-8")
        paper = parse_ts_to_dict(ts_code)

        # Include LinkedIn posts if available
        linkedin_posts = get_linkedin_posts(slug) if source == "local" else {}

        return JSONResponse({
            "success": True,
            "paper": paper,
            "linkedin_posts": linkedin_posts,
        })
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Parse error: {e}", "paper": {}, "linkedin_posts": {}})


@app.post("/api/article/{slug}/edit")
def api_article_edit(slug: str, body: ArticleEditRequest, _: dict = Depends(require_auth)):
    """
    Save an edited article. Two modes:
    - body.paper provided: serialize dict → TS, validate, write file
    - body.ts_raw provided: validate raw TS, write file directly
    """
    from modules.article_editor import serialize_dict_to_ts, validate_paper_dict
    from modules.publisher import _validate_before_publish

    ts_path = OUTPUT_DIR / "articles" / f"{slug}.ts"
    ts_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if body.ts_raw:
            # Raw TS mode
            errors = _validate_before_publish(body.ts_raw, slug)
            if errors:
                return JSONResponse({"success": False, "slug": slug, "message": " | ".join(errors), "validation_errors": errors})
            ts_path.write_text(body.ts_raw, encoding="utf-8")
        else:
            # Dict mode
            validation_errors = validate_paper_dict(body.paper)
            new_ts = serialize_dict_to_ts(body.paper)
            pub_errors = _validate_before_publish(new_ts, slug)
            all_errors = validation_errors + pub_errors
            ts_path.write_text(new_ts, encoding="utf-8")
            return JSONResponse({
                "success": True,
                "slug": slug,
                "message": "Article saved successfully",
                "validation_errors": all_errors,
            })
        return JSONResponse({"success": True, "slug": slug, "message": "Article saved successfully", "validation_errors": []})
    except Exception as e:
        return JSONResponse({"success": False, "slug": slug, "message": f"Save error: {e}", "validation_errors": []})


@app.post("/api/article/{slug}/rename")
def api_article_rename(slug: str, body: RenameRequest, _: dict = Depends(require_auth)):
    """Rename an article's slug + title + all associated files."""
    from modules.article_editor import rename_article
    result = rename_article(slug, body.new_title, OUTPUT_DIR)
    return JSONResponse(result)


@app.post("/api/article/{slug}/regenerate-linkedin")
def api_regenerate_linkedin(slug: str, _: dict = Depends(require_auth)):
    """Regenerate LinkedIn posts for an article using the existing linkedin_generator."""
    from modules.linkedin_generator import generate_linkedin_posts, save_linkedin_posts

    ts_path = OUTPUT_DIR / "articles" / f"{slug}.ts"
    if not ts_path.exists():
        return JSONResponse({"success": False, "message": f"Article not found: {slug}", "posts": {}})

    try:
        ts_code = ts_path.read_text(encoding="utf-8")
        article = {"slug": slug, "ts_code": ts_code, "title": slug}

        # Extract title from TS
        import re
        m = re.search(r'title:\s*"([^"]+)"', ts_code)
        if m:
            article["title"] = m.group(1)

        variants = generate_linkedin_posts(article)
        save_linkedin_posts(article, variants, OUTPUT_DIR / "linkedin")
        return JSONResponse({"success": True, "posts": variants})
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Regeneration error: {e}", "posts": {}})


# ── NEW: LinkedIn Hub API ──────────────────────────────────────────────────────

@app.get("/api/linkedin-hub")
def api_linkedin_hub(_: dict = Depends(require_auth)):
    """Return all LinkedIn posts for all local articles."""
    articles = get_pending_articles()
    result = []
    for a in articles:
        posts = get_linkedin_posts(a["slug"])
        if posts:
            result.append({
                "slug": a["slug"],
                "title": a["title"],
                "posts": posts,
                "has_all": bool(posts.get("hook") and posts.get("insight") and posts.get("story")),
            })
    return JSONResponse({"articles": result})


# ── NEW: Stats API ─────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats(_: dict = Depends(require_auth)):
    """Return pipeline + publication statistics."""
    published = _load_published()
    last_run = _load_last_run()
    articles_dir = OUTPUT_DIR / "articles"
    pending_count = len(list(articles_dir.glob("*.ts"))) if articles_dir.exists() else 0

    history = [
        {"slug": slug, "published_at": ts}
        for slug, ts in sorted(published.items(), key=lambda x: x[1], reverse=True)
    ]

    return JSONResponse({
        "published_count": len(published),
        "pending_count": pending_count,
        "last_run": last_run,
        "published_history": history,
    })
