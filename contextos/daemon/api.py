"""
daemon/api.py — Lightweight local HTTP dashboard API for ContextOS.

Serves JSON endpoints and a rich single-page HTML dashboard.
Designed to be started by DaemonObserver as a background thread and stopped
cleanly on daemon shutdown. No external dependencies beyond the stdlib.

Endpoints
---------
  GET /                      -> HTML dashboard SPA
  GET /api/status            -> daemon heartbeat + DB counts
  GET /api/sessions          -> paginated session list
  GET /api/sessions/<id>     -> single session with events
  GET /api/events            -> recent events (paginated, filterable by project)
  GET /api/projects          -> list of registered projects
  GET /api/health            -> latest daemon health snapshot
  GET /api/summaries         -> recent summaries (final + mini)
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from contextos.core.config import settings
from contextos.core.database import get_db_conn

logger = logging.getLogger("contextos.daemon.api")

# ---------------------------------------------------------------------------
# HTML Dashboard (inline SPA)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ContextOS — Local Dashboard</title>
  <meta name="description" content="ContextOS local developer memory dashboard — browse sessions, events, and AI-generated summaries." />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    /* ---- DESIGN TOKENS ---- */
    :root {
      --bg:         #0d0f17;
      --surface:    #131620;
      --surface2:   #1a1e2e;
      --border:     #252a3a;
      --accent:     #7c5cfc;
      --accent2:    #5be0c0;
      --accent3:    #fc5c7d;
      --text:       #e8eaf6;
      --text-muted: #7b82a0;
      --success:    #4ade80;
      --warning:    #fbbf24;
      --error:      #f87171;
      --radius:     10px;
      --shadow:     0 4px 24px rgba(0,0,0,0.45);
      --glow:       0 0 20px rgba(124,92,252,0.18);
    }

    /* ---- RESET & BASE ---- */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body {
      font-family: 'Inter', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      line-height: 1.6;
      overflow-x: hidden;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { color: var(--accent2); }

    /* ---- LAYOUT ---- */
    .app { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }

    /* ---- SIDEBAR ---- */
    .sidebar {
      background: var(--surface);
      border-right: 1px solid var(--border);
      padding: 0;
      display: flex;
      flex-direction: column;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
    }
    .sidebar-logo {
      padding: 28px 24px 20px;
      border-bottom: 1px solid var(--border);
    }
    .sidebar-logo h1 {
      font-size: 1.25rem;
      font-weight: 700;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      letter-spacing: -0.02em;
    }
    .sidebar-logo .tagline {
      font-size: 0.72rem;
      color: var(--text-muted);
      margin-top: 2px;
    }
    .sidebar-status {
      padding: 12px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.78rem;
      color: var(--text-muted);
    }
    .status-dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 6px var(--success);
      flex-shrink: 0;
      animation: pulse-dot 2s infinite;
    }
    .status-dot.offline { background: var(--error); box-shadow: 0 0 6px var(--error); animation: none; }
    @keyframes pulse-dot {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    .nav { padding: 16px 12px; flex: 1; }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: var(--radius);
      cursor: pointer;
      transition: all 0.18s ease;
      font-size: 0.88rem;
      font-weight: 500;
      color: var(--text-muted);
      border: none;
      background: none;
      width: 100%;
      text-align: left;
    }
    .nav-item:hover { background: var(--surface2); color: var(--text); }
    .nav-item.active {
      background: rgba(124,92,252,0.15);
      color: var(--accent);
      box-shadow: inset 2px 0 0 var(--accent);
    }
    .nav-item .icon { font-size: 1rem; width: 18px; text-align: center; }
    .sidebar-footer {
      padding: 16px 24px;
      border-top: 1px solid var(--border);
      font-size: 0.72rem;
      color: var(--text-muted);
    }
    .sidebar-footer .ver { color: var(--accent); font-weight: 600; }

    /* ---- MAIN ---- */
    .main {
      display: flex;
      flex-direction: column;
      overflow-y: auto;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 32px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .topbar h2 {
      font-size: 1.1rem;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    .topbar-actions { display: flex; align-items: center; gap: 12px; }
    .btn {
      padding: 7px 16px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: var(--surface2);
      color: var(--text);
      font-size: 0.82rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s;
    }
    .btn:hover { border-color: var(--accent); color: var(--accent); }
    .btn.primary {
      background: linear-gradient(135deg, var(--accent), #6347d9);
      border-color: var(--accent);
      color: #fff;
      box-shadow: 0 2px 10px rgba(124,92,252,0.3);
    }
    .btn.primary:hover { opacity: 0.88; }
    .last-refreshed { font-size: 0.75rem; color: var(--text-muted); }

    /* ---- CONTENT AREA ---- */
    .content { padding: 28px 32px; flex: 1; }

    /* ---- STAT CARDS ---- */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .stat-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px;
      position: relative;
      overflow: hidden;
      transition: transform 0.2s, box-shadow 0.2s;
    }
    .stat-card:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
    .stat-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 2px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
    }
    .stat-card.warn::before { background: linear-gradient(90deg, var(--warning), var(--accent3)); }
    .stat-card.ok::before   { background: linear-gradient(90deg, var(--success), var(--accent2)); }
    .stat-label { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.07em; font-weight: 600; }
    .stat-value { font-size: 2rem; font-weight: 700; margin-top: 6px; letter-spacing: -0.03em; }
    .stat-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; }

    /* ---- SECTION HEADER ---- */
    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    .section-title {
      font-size: 0.95rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 20px;
      font-size: 0.7rem;
      font-weight: 600;
      background: rgba(124,92,252,0.18);
      color: var(--accent);
    }
    .badge.green { background: rgba(74,222,128,0.15); color: var(--success); }
    .badge.yellow { background: rgba(251,191,36,0.15); color: var(--warning); }

    /* ---- TABLES ---- */
    .table-wrap {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      margin-bottom: 24px;
      box-shadow: var(--shadow);
    }
    table { width: 100%; border-collapse: collapse; }
    thead { background: var(--surface2); }
    th {
      text-align: left;
      padding: 11px 16px;
      font-size: 0.73rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 12px 16px;
      font-size: 0.85rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      transition: background 0.15s;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: var(--surface2); }
    .monospace { font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; }
    .text-muted { color: var(--text-muted); }
    .truncate { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    /* ---- STATUS PILLS ---- */
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 10px;
      border-radius: 20px;
      font-size: 0.72rem;
      font-weight: 600;
    }
    .pill.active   { background: rgba(74,222,128,0.14); color: var(--success); border: 1px solid rgba(74,222,128,0.3); }
    .pill.completed{ background: rgba(124,92,252,0.14); color: var(--accent);  border: 1px solid rgba(124,92,252,0.3); }
    .pill.mini     { background: rgba(251,191,36,0.14);  color: var(--warning); border: 1px solid rgba(251,191,36,0.3); }
    .pill.final    { background: rgba(91,224,192,0.14); color: var(--accent2); border: 1px solid rgba(91,224,192,0.3); }

    /* ---- EXPANDABLE ROWS ---- */
    .expand-btn {
      background: none; border: 1px solid var(--border);
      border-radius: 5px; color: var(--text-muted);
      cursor: pointer; font-size: 0.7rem;
      padding: 2px 8px; transition: all 0.15s;
    }
    .expand-btn:hover { border-color: var(--accent); color: var(--accent); }
    .expanded-content {
      display: none;
      background: var(--surface2);
      border-top: 1px solid var(--border);
      padding: 12px 16px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.78rem;
      white-space: pre-wrap;
      color: var(--accent2);
      line-height: 1.5;
    }
    .expanded-content.visible { display: block; }

    /* ---- SEARCH BAR ---- */
    .search-bar {
      display: flex;
      gap: 10px;
      margin-bottom: 20px;
    }
    .search-input {
      flex: 1;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 9px 14px;
      color: var(--text);
      font-size: 0.88rem;
      font-family: inherit;
      outline: none;
      transition: border-color 0.15s;
    }
    .search-input:focus { border-color: var(--accent); }
    .search-input::placeholder { color: var(--text-muted); }

    /* ---- HEALTH BAR ---- */
    .health-bar-wrap { display: flex; align-items: center; gap: 8px; }
    .health-bar {
      flex: 1; height: 6px;
      background: var(--border);
      border-radius: 3px;
      overflow: hidden;
    }
    .health-bar-fill {
      height: 100%;
      border-radius: 3px;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      transition: width 0.5s ease;
    }

    /* ---- EMPTY STATE ---- */
    .empty-state {
      text-align: center;
      padding: 48px 24px;
      color: var(--text-muted);
    }
    .empty-state .icon { font-size: 2.5rem; margin-bottom: 12px; }
    .empty-state p { font-size: 0.9rem; }

    /* ---- LOADING ---- */
    .spinner {
      display: inline-block;
      width: 18px; height: 18px;
      border: 2px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .loading-overlay {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 40px;
      gap: 12px;
      color: var(--text-muted);
      font-size: 0.85rem;
    }

    /* ---- PAGE TRANSITIONS ---- */
    .page { display: none; animation: fadeIn 0.25s ease; }
    .page.active { display: block; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

    /* ---- SCROLLBAR ---- */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

    /* ---- RESPONSIVE ---- */
    @media (max-width: 768px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { position: relative; height: auto; flex-direction: row; flex-wrap: wrap; }
      .content { padding: 16px; }
      .stats-grid { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>
<div class="app" id="app">
  <!-- SIDEBAR -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-logo">
      <h1>🧠 ContextOS</h1>
      <div class="tagline">Local Developer Memory</div>
    </div>
    <div class="sidebar-status" id="daemon-status-badge">
      <div class="status-dot" id="status-dot"></div>
      <span id="status-text">Connecting…</span>
    </div>
    <nav class="nav">
      <button class="nav-item active" id="nav-overview" onclick="navigate('overview')">
        <span class="icon">📊</span> Overview
      </button>
      <button class="nav-item" id="nav-sessions" onclick="navigate('sessions')">
        <span class="icon">📁</span> Sessions
      </button>
      <button class="nav-item" id="nav-events" onclick="navigate('events')">
        <span class="icon">⚡</span> Live Events
      </button>
      <button class="nav-item" id="nav-summaries" onclick="navigate('summaries')">
        <span class="icon">📝</span> Summaries
      </button>
      <button class="nav-item" id="nav-projects" onclick="navigate('projects')">
        <span class="icon">🗂️</span> Projects
      </button>
      <button class="nav-item" id="nav-health" onclick="navigate('health')">
        <span class="icon">💚</span> Health
      </button>
      <button class="nav-item" id="nav-settings" onclick="navigate('settings')">
        <span class="icon">⚙️</span> Settings
      </button>
    </nav>
    <div class="sidebar-footer">
      ContextOS <span class="ver">v0.6.1</span><br/>
      <span id="sidebar-event-count">—</span> events recorded
    </div>
  </aside>

  <!-- MAIN -->
  <main class="main" id="main-content">
    <div class="topbar">
      <h2 id="page-title">Overview</h2>
      <div class="topbar-actions">
        <span class="last-refreshed" id="last-refreshed">–</span>
        <button class="btn primary" onclick="refreshAll()">↺ Refresh</button>
      </div>
    </div>
    <div class="content">

      <!-- OVERVIEW -->
      <div class="page active" id="page-overview">
        <div class="stats-grid" id="stats-grid">
          <div class="loading-overlay"><div class="spinner"></div> Loading…</div>
        </div>
        <div class="section-header">
          <div class="section-title">📁 Recent Sessions <span class="badge" id="active-count">…</span></div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Project</th><th>Started</th><th>Ended</th><th>Status</th><th>Summary</th>
            </tr></thead>
            <tbody id="overview-sessions"></tbody>
          </table>
        </div>
      </div>

      <!-- SESSIONS -->
      <div class="page" id="page-sessions">
        <div class="search-bar">
          <input class="search-input" id="session-filter" placeholder="Filter by project name…" oninput="filterSessions()" />
          <button class="btn" onclick="loadSessions()">↺</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>ID</th><th>Project</th><th>Started</th><th>Ended</th><th>Status</th><th>Summary</th>
            </tr></thead>
            <tbody id="sessions-table"></tbody>
          </table>
        </div>
      </div>

      <!-- EVENTS -->
      <div class="page" id="page-events">
        <div class="search-bar">
          <input class="search-input" id="event-project-filter" placeholder="Filter by project name…" oninput="loadEvents()" />
          <button class="btn" onclick="loadEvents()">↺</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Time</th><th>Project</th><th>Source</th><th>Type</th><th>File</th><th>Payload</th>
            </tr></thead>
            <tbody id="events-table"></tbody>
          </table>
        </div>
      </div>

      <!-- SUMMARIES -->
      <div class="page" id="page-summaries">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Project</th><th>Type</th><th>Time</th><th>Summary</th>
            </tr></thead>
            <tbody id="summaries-table"></tbody>
          </table>
        </div>
      </div>

      <!-- PROJECTS -->
      <div class="page" id="page-projects">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Name</th><th>Path</th><th>Status</th><th>Created</th>
            </tr></thead>
            <tbody id="projects-table"></tbody>
          </table>
        </div>
      </div>

      <!-- HEALTH -->
      <div class="page" id="page-health">
        <div class="stats-grid" id="health-stats"></div>
        <div class="section-header" style="margin-top:8px;">
          <div class="section-title">📈 Health History</div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Time</th><th>CPU %</th><th>RAM (MB)</th><th>Threads</th>
            </tr></thead>
            <tbody id="health-table"></tbody>
          </table>
        </div>
      </div>

      </div>

      <!-- SETTINGS -->
      <div class="page" id="page-settings">
        <div style="max-width:540px;margin:0 auto;padding:8px 0;">
          <div class="section-header"><div class="section-title">🔑 LLM Configuration</div></div>
          <div id="settings-status" style="margin-bottom:16px;color:var(--text-muted);font-size:0.88rem;">Loading…</div>
          <div style="background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:24px;">
            <p style="font-size:0.88rem;color:var(--text-muted);margin-bottom:18px;">
              Add an <strong>OpenRouter</strong> or <strong>Google Gemini</strong> API key to enable AI-written session summaries and natural language answers.<br/>
              The key is saved to <code style="color:var(--accent2)">~/.contextos/.env</code> and never leaves your machine.
            </p>
            <label style="display:block;font-size:0.82rem;font-weight:600;margin-bottom:6px;color:var(--text-muted);">API Key</label>
            <input id="settings-api-key" type="password" placeholder="sk-or-… or AIza…"
              style="width:100%;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.9rem;font-family:inherit;outline:none;"
              onfocus="this.style.borderColor='var(--accent)'" onblur="this.style.borderColor='var(--border)'"/>
            <p style="font-size:0.75rem;color:var(--text-muted);margin-top:6px;">
              Starts with <code>sk-or-</code> = OpenRouter &nbsp;·&nbsp; Starts with <code>AIza</code> = Gemini
            </p>
            <button class="btn primary" onclick="saveSettings()" style="margin-top:18px;width:100%;padding:10px;font-size:0.9rem;">Save API Key</button>
            <div id="settings-save-msg" style="margin-top:12px;font-size:0.83rem;"></div>
          </div>

          <div style="margin-top:24px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:20px;">
            <div class="section-title" style="margin-bottom:12px;">🦙 Ollama (Local Model)</div>
            <p style="font-size:0.85rem;color:var(--text-muted);margin-bottom:14px;">If you run Ollama locally, ContextOS can use it instead. Set the model name below.</p>
            <label style="display:block;font-size:0.82rem;font-weight:600;margin-bottom:6px;color:var(--text-muted);">Ollama Model</label>
            <input id="settings-ollama" type="text" placeholder="e.g. llama3.2"
              style="width:100%;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:0.9rem;font-family:inherit;outline:none;"
              onfocus="this.style.borderColor='var(--accent)'" onblur="this.style.borderColor='var(--border)'"/>
            <button class="btn" onclick="saveOllama()" style="margin-top:14px;width:100%;padding:10px;font-size:0.9rem;">Save Ollama Model</button>
          </div>
        </div>
      </div>

    </div><!-- /content -->
  </main>
</div>

<script>
/* ===== STATE ===== */
let currentPage = 'overview';
let allSessions = [];
let refreshTimer = null;

const PAGE_TITLES = {
  overview: 'Overview',
  sessions: 'Sessions',
  events: 'Live Events',
  summaries: 'Summaries',
  projects: 'Projects',
  health: 'Health',
  settings: 'Settings',
};

/* ===== NAVIGATION ===== */
function navigate(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  document.getElementById('nav-' + page).classList.add('active');
  document.getElementById('page-title').textContent = PAGE_TITLES[page] || page;
  currentPage = page;
  loadPage(page);
}

function loadPage(page) {
  if (page === 'overview') { loadOverview(); }
  else if (page === 'sessions') { loadSessions(); }
  else if (page === 'events') { loadEvents(); }
  else if (page === 'summaries') { loadSummaries(); }
  else if (page === 'projects') { loadProjects(); }
  else if (page === 'health') { loadHealth(); }
  else if (page === 'settings') { loadSettings(); }
}

/* ===== API HELPERS ===== */
async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function ts(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
  } catch { return iso.slice(0,16).replace('T',' '); }
}

function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function setLoading(id) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = `<tr><td colspan="10"><div class="loading-overlay"><div class="spinner"></div> Loading…</div></td></tr>`;
}

function setEmpty(id, msg='No data yet.') {
  const el = document.getElementById(id);
  if (el) el.innerHTML = `<tr><td colspan="10"><div class="empty-state"><div class="icon">🔍</div><p>${escHtml(msg)}</p></div></td></tr>`;
}

/* ===== DAEMON STATUS ===== */
async function checkStatus() {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  try {
    const data = await api('/api/status');
    dot.className = 'status-dot';
    txt.textContent = `Running · PID ${data.pid || '?'}`;
    document.getElementById('sidebar-event-count').textContent =
      (data.total_events ?? 0).toLocaleString();
    document.getElementById('last-refreshed').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch {
    dot.className = 'status-dot offline';
    txt.textContent = 'Daemon offline';
  }
}

/* ===== OVERVIEW ===== */
async function loadOverview() {
  const grid = document.getElementById('stats-grid');
  grid.innerHTML = `<div class="loading-overlay"><div class="spinner"></div> Loading…</div>`;
  try {
    const [status, sessions] = await Promise.all([
      api('/api/status'),
      api('/api/sessions?limit=8'),
    ]);

    const active = (sessions.sessions || []).filter(s => s.status === 'ACTIVE').length;
    document.getElementById('active-count').textContent = active + ' active';

    grid.innerHTML = `
      ${statCard('Total Events', (status.total_events||0).toLocaleString(), 'filesystem + git + terminal', '')}
      ${statCard('Sessions', (status.total_sessions||0).toLocaleString(), (active||0) + ' currently active', active > 0 ? 'ok' : '')}
      ${statCard('Projects', (status.total_projects||0).toLocaleString(), 'registered paths', '')}
      ${statCard('Vector Docs', (status.total_vector_docs||0).toLocaleString(), 'indexed in sqlite-vec', '')}
    `;

    const tbody = document.getElementById('overview-sessions');
    renderSessionsInto(tbody, sessions.sessions || []);
  } catch(e) {
    grid.innerHTML = `<div class="empty-state"><p>Could not load data: ${escHtml(e.message)}</p></div>`;
  }
}

function statCard(label, value, sub='', type='') {
  return `<div class="stat-card ${type}">
    <div class="stat-label">${escHtml(label)}</div>
    <div class="stat-value">${escHtml(value)}</div>
    <div class="stat-sub">${escHtml(sub)}</div>
  </div>`;
}

/* ===== SESSIONS ===== */
async function loadSessions() {
  setLoading('sessions-table');
  try {
    const data = await api('/api/sessions?limit=100');
    allSessions = data.sessions || [];
    renderSessions(allSessions);
  } catch(e) {
    setEmpty('sessions-table', 'Failed to load sessions: ' + e.message);
  }
}

function filterSessions() {
  const q = document.getElementById('session-filter').value.toLowerCase();
  const filtered = allSessions.filter(s =>
    (s.project_name || '').toLowerCase().includes(q) ||
    (s.session_id || '').toLowerCase().includes(q)
  );
  renderSessions(filtered);
}

function renderSessions(sessions) {
  const tbody = document.getElementById('sessions-table');
  if (!sessions.length) { setEmpty('sessions-table', 'No sessions yet.'); return; }
  tbody.innerHTML = sessions.map(s => {
    const shortId = (s.session_id || '').slice(0,8);
    const pill = s.status === 'ACTIVE'
      ? `<span class="pill active">● Active</span>`
      : `<span class="pill completed">✓ Done</span>`;
    const sumText = s.summary ? s.summary.slice(0,80) + (s.summary.length > 80 ? '…' : '') : '—';
    return `<tr>
      <td class="monospace text-muted">${escHtml(shortId)}…</td>
      <td><strong>${escHtml(s.project_name)}</strong></td>
      <td class="text-muted">${ts(s.start_time)}</td>
      <td class="text-muted">${ts(s.end_time)}</td>
      <td>${pill}</td>
      <td class="truncate text-muted">${escHtml(sumText)}</td>
    </tr>`;
  }).join('');
}

function renderSessionsInto(tbody, sessions) {
  if (!sessions.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><div class="icon">🔍</div><p>No sessions yet.</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = sessions.map(s => {
    const pill = s.status === 'ACTIVE'
      ? `<span class="pill active">● Active</span>`
      : `<span class="pill completed">✓ Done</span>`;
    const sumText = s.summary ? s.summary.slice(0,80) + (s.summary.length > 80 ? '…' : '') : '—';
    return `<tr>
      <td><strong>${escHtml(s.project_name)}</strong></td>
      <td class="text-muted">${ts(s.start_time)}</td>
      <td class="text-muted">${ts(s.end_time)}</td>
      <td>${pill}</td>
      <td class="truncate text-muted">${escHtml(sumText)}</td>
    </tr>`;
  }).join('');
}

/* ===== EVENTS ===== */
async function loadEvents() {
  setLoading('events-table');
  const proj = document.getElementById('event-project-filter').value.trim();
  const url = '/api/events?limit=100' + (proj ? '&project=' + encodeURIComponent(proj) : '');
  try {
    const data = await api(url);
    const events = data.events || [];
    if (!events.length) { setEmpty('events-table', 'No events found.'); return; }
    let idx = 0;
    document.getElementById('events-table').innerHTML = events.map(e => {
      const id = `ev-${idx++}`;
      const hasPayload = e.payload && e.payload !== 'null';
      const payloadBtn = hasPayload
        ? `<button class="expand-btn" onclick="toggleExpand('${id}')">view</button>`
        : '<span class="text-muted">—</span>';
      return `<tr>
        <td class="monospace text-muted">${escHtml((e.timestamp||'').slice(0,19).replace('T',' '))}</td>
        <td>${escHtml(e.project_name)}</td>
        <td><span class="badge">${escHtml(e.source)}</span></td>
        <td>${escHtml(e.event_type)}</td>
        <td class="monospace truncate" style="max-width:200px">${escHtml(e.file_path)}</td>
        <td>${payloadBtn}</td>
      </tr>
      ${hasPayload ? `<tr id="${id}" class="expanded-content">${escHtml(prettyPayload(e.payload))}</tr>` : ''}`;
    }).join('');
  } catch(e) {
    setEmpty('events-table', 'Failed to load events: ' + e.message);
  }
}

function prettyPayload(p) {
  try { return JSON.stringify(JSON.parse(p), null, 2); } catch { return p; }
}

function toggleExpand(id) {
  const row = document.getElementById(id);
  if (row) row.classList.toggle('visible');
}

/* ===== SUMMARIES ===== */
async function loadSummaries() {
  setLoading('summaries-table');
  try {
    const data = await api('/api/summaries?limit=50');
    const summaries = data.summaries || [];
    if (!summaries.length) { setEmpty('summaries-table', 'No summaries yet. Run the daemon for a session!'); return; }
    document.getElementById('summaries-table').innerHTML = summaries.map(s => {
      const pill = s.type === 'final'
        ? `<span class="pill final">Final</span>`
        : `<span class="pill mini">Mini</span>`;
      const text = (s.summary || '').slice(0, 120) + ((s.summary||'').length > 120 ? '…' : '');
      return `<tr>
        <td><strong>${escHtml(s.project_name)}</strong></td>
        <td>${pill}</td>
        <td class="text-muted">${ts(s.timestamp)}</td>
        <td class="truncate">${escHtml(text)}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    setEmpty('summaries-table', 'Failed to load summaries: ' + e.message);
  }
}

/* ===== PROJECTS ===== */
async function loadProjects() {
  setLoading('projects-table');
  try {
    const data = await api('/api/projects');
    const projects = data.projects || [];
    if (!projects.length) { setEmpty('projects-table', 'No projects registered yet.'); return; }
    document.getElementById('projects-table').innerHTML = projects.map(p => `<tr>
      <td><strong>${escHtml(p.name)}</strong></td>
      <td class="monospace text-muted truncate">${escHtml(p.path)}</td>
      <td><span class="badge">${escHtml(p.status || 'IDLE')}</span></td>
      <td class="text-muted">${ts(p.created_at)}</td>
    </tr>`).join('');
  } catch(e) {
    setEmpty('projects-table', 'Failed to load projects: ' + e.message);
  }
}

/* ===== HEALTH ===== */
async function loadHealth() {
  const grid = document.getElementById('health-stats');
  grid.innerHTML = `<div class="loading-overlay"><div class="spinner"></div> Loading…</div>`;
  try {
    const data = await api('/api/health');
    const snap = data.latest || {};
    const rss = snap.memory_rss_bytes ? (snap.memory_rss_bytes / 1048576).toFixed(1) : '—';
    const cpu = snap.cpu_percent != null ? snap.cpu_percent.toFixed(1) + '%' : '—';
    const threads = snap.thread_count ?? '—';
    grid.innerHTML = `
      ${statCard('CPU', cpu, 'daemon process', parseFloat(cpu) > 10 ? 'warn' : 'ok')}
      ${statCard('RAM', rss + ' MB', 'resident set size', '')}
      ${statCard('Threads', String(threads), 'active threads', '')}
      ${statCard('Open Files', String(snap.open_files ?? '—'), 'file descriptors', '')}
    `;

    const history = data.history || [];
    if (!history.length) { setEmpty('health-table', 'No health snapshots yet.'); return; }
    document.getElementById('health-table').innerHTML = history.map(h => {
      const mb = h.memory_rss_bytes ? (h.memory_rss_bytes / 1048576).toFixed(1) : '—';
      return `<tr>
        <td class="text-muted">${ts(h.timestamp)}</td>
        <td>${h.cpu_percent != null ? h.cpu_percent.toFixed(1) + '%' : '—'}</td>
        <td>${mb} MB</td>
        <td>${h.thread_count ?? '—'}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    grid.innerHTML = `<div class="empty-state"><p>Health data unavailable: ${escHtml(e.message)}</p></div>`;
  }
}

/* ===== AUTO-REFRESH ===== */
function refreshAll() {
  checkStatus();
  loadPage(currentPage);
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshAll, 15000);
}

/* ===== SETTINGS ===== */
async function loadSettings() {
  const statusEl = document.getElementById('settings-status');
  try {
    const data = await api('/api/settings');
    let providerText = 'No API key configured. AI features are disabled (offline mode).';
    let providerColor = 'var(--warning)';
    if (data.has_openrouter_key) {
      providerText = '✅ OpenRouter API key is configured.';
      providerColor = 'var(--success)';
    } else if (data.has_gemini_key) {
      providerText = '✅ Google Gemini API key is configured.';
      providerColor = 'var(--success)';
    }
    statusEl.innerHTML = `<span style="color:${providerColor}">${escHtml(providerText)}</span>`;
    const ollamaEl = document.getElementById('settings-ollama');
    if (ollamaEl && data.ollama_model) ollamaEl.value = data.ollama_model;
  } catch(e) {
    statusEl.textContent = 'Could not load settings: ' + e.message;
  }
}

async function saveSettings() {
  const key = (document.getElementById('settings-api-key').value || '').trim();
  const msgEl = document.getElementById('settings-save-msg');
  if (!key) { msgEl.style.color = 'var(--warning)'; msgEl.textContent = 'Enter an API key first.'; return; }
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_key: key})
    });
    const data = await res.json();
    if (data.ok) {
      msgEl.style.color = 'var(--success)';
      msgEl.textContent = '✅ Saved! Restart the daemon for the key to take effect.';
      document.getElementById('settings-api-key').value = '';
      loadSettings();
    } else {
      msgEl.style.color = 'var(--error)';
      msgEl.textContent = data.error || 'Save failed.';
    }
  } catch(e) {
    msgEl.style.color = 'var(--error)';
    msgEl.textContent = 'Error: ' + e.message;
  }
}

async function saveOllama() {
  const model = (document.getElementById('settings-ollama').value || '').trim();
  const msgEl = document.getElementById('settings-save-msg');
  if (!model) { msgEl.style.color = 'var(--warning)'; msgEl.textContent = 'Enter an Ollama model name.'; return; }
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ollama_model: model})
    });
    const data = await res.json();
    if (data.ok) {
      msgEl.style.color = 'var(--success)';
      msgEl.textContent = '✅ Ollama model saved. Restart the daemon to apply.';
    } else {
      msgEl.style.color = 'var(--error)';
      msgEl.textContent = data.error || 'Save failed.';
    }
  } catch(e) {
    msgEl.style.color = 'var(--error)';
    msgEl.textContent = 'Error: ' + e.message;
  }
}

/* ===== INIT ===== */
checkStatus();
loadPage('overview');
startAutoRefresh();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _json_response(data: Any) -> bytes:
    return json.dumps(data, default=str).encode("utf-8")


def _parse_int(value: str | None, default: int) -> int:
    try:
        return max(1, int(value)) if value else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Request Handler
# ---------------------------------------------------------------------------

class ContextOSRequestHandler(BaseHTTPRequestHandler):
    """Handles all dashboard HTTP requests."""

    def log_message(self, fmt: str, *args) -> None:  # suppress default access log
        logger.debug("API: " + fmt, *args)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: Any, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", _json_response(data))

    def _send_html(self, html: str, status: int = 200) -> None:
        self._send(status, "text/html; charset=utf-8", html.encode("utf-8"))

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        routes = {
            "/":               self._handle_dashboard,
            "/api/status":     self._handle_status,
            "/api/sessions":   self._handle_sessions,
            "/api/events":     self._handle_events,
            "/api/projects":   self._handle_projects,
            "/api/health":     self._handle_health,
            "/api/summaries":  self._handle_summaries,
            "/api/settings":   self._handle_settings_get,
        }

        handler = routes.get(path)
        if handler:
            try:
                handler(qs)
            except Exception as exc:
                logger.exception("API handler error: %s", exc)
                self._send_error_json(500, str(exc))
        elif path.startswith("/api/sessions/"):
            session_id = path.split("/api/sessions/", 1)[1]
            try:
                self._handle_single_session(session_id, qs)
            except Exception as exc:
                logger.exception("API handler error: %s", exc)
                self._send_error_json(500, str(exc))
        else:
            self._send_error_json(404, f"Not found: {path}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if path == "/api/settings":
            try:
                self._handle_settings_post(data)
            except Exception as exc:
                logger.exception("Settings POST error: %s", exc)
                self._send_error_json(500, str(exc))
        else:
            self._send_error_json(404, f"Not found: {path}")

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    def _handle_dashboard(self, qs: dict) -> None:
        self._send_html(_DASHBOARD_HTML)

    def _handle_status(self, qs: dict) -> None:
        try:
            with get_db_conn() as conn:
                total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                total_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                try:
                    total_vector_docs = conn.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0]
                except Exception:
                    total_vector_docs = 0
                active_sessions = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'"
                ).fetchone()[0]
        except Exception as exc:
            self._send_error_json(503, f"Database unavailable: {exc}")
            return

        pid_data: dict[str, Any] = {}
        pid_file = settings.PID_FILE
        if pid_file.exists():
            try:
                pid_data = json.loads(pid_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        self._send_json({
            "status": "running",
            "pid": pid_data.get("pid"),
            "started_at": pid_data.get("started_at"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_events": total_events,
            "total_sessions": total_sessions,
            "total_projects": total_projects,
            "total_vector_docs": total_vector_docs,
            "active_sessions": active_sessions,
            "db_path": str(settings.DB_PATH),
        })

    def _handle_sessions(self, qs: dict) -> None:
        limit = _parse_int((qs.get("limit") or [None])[0], 50)
        try:
            with get_db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id, project_name, start_time, end_time, status, summary
                    FROM sessions
                    ORDER BY start_time DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                sessions = [dict(r) for r in rows]
        except Exception as exc:
            self._send_error_json(503, str(exc))
            return
        self._send_json({"sessions": sessions, "count": len(sessions)})

    def _handle_single_session(self, session_id: str, qs: dict) -> None:
        try:
            with get_db_conn() as conn:
                row = conn.execute(
                    "SELECT session_id, project_name, start_time, end_time, status, summary "
                    "FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    self._send_error_json(404, f"Session {session_id!r} not found")
                    return

                session = dict(row)
                events = conn.execute(
                    """
                    SELECT timestamp, source, event_type, file_path, payload
                    FROM events
                    WHERE project_name = ? AND timestamp >= ?
                    ORDER BY timestamp ASC
                    LIMIT 500
                    """,
                    (row["project_name"], row["start_time"]),
                ).fetchall()
                session["events"] = [dict(e) for e in events]
        except Exception as exc:
            self._send_error_json(503, str(exc))
            return
        self._send_json(session)

    def _handle_events(self, qs: dict) -> None:
        limit = _parse_int((qs.get("limit") or [None])[0], 100)
        project = (qs.get("project") or [None])[0]
        try:
            with get_db_conn() as conn:
                if project:
                    rows = conn.execute(
                        """
                        SELECT timestamp, project_name, source, event_type, file_path, payload
                        FROM events
                        WHERE project_name = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (project, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT timestamp, project_name, source, event_type, file_path, payload
                        FROM events
                        ORDER BY timestamp DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                events = [dict(r) for r in rows]
        except Exception as exc:
            self._send_error_json(503, str(exc))
            return
        self._send_json({"events": events, "count": len(events)})

    def _handle_projects(self, qs: dict) -> None:
        try:
            with get_db_conn() as conn:
                rows = conn.execute(
                    "SELECT name, path, status, created_at FROM projects ORDER BY created_at DESC"
                ).fetchall()
                projects = [dict(r) for r in rows]
        except Exception as exc:
            self._send_error_json(503, str(exc))
            return
        self._send_json({"projects": projects, "count": len(projects)})

    def _handle_health(self, qs: dict) -> None:
        try:
            with get_db_conn() as conn:
                latest_row = conn.execute(
                    """
                    SELECT timestamp, pid, cpu_percent, memory_rss_bytes, memory_percent,
                           thread_count, open_files
                    FROM daemon_health
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """
                ).fetchone()
                history_rows = conn.execute(
                    """
                    SELECT timestamp, cpu_percent, memory_rss_bytes, thread_count
                    FROM daemon_health
                    ORDER BY timestamp DESC
                    LIMIT 60
                    """
                ).fetchall()
                latest = dict(latest_row) if latest_row else {}
                history = [dict(r) for r in history_rows]
        except Exception as exc:
            self._send_error_json(503, str(exc))
            return
        self._send_json({"latest": latest, "history": history})

    def _handle_summaries(self, qs: dict) -> None:
        limit = _parse_int((qs.get("limit") or [None])[0], 50)
        try:
            with get_db_conn() as conn:
                # Final summaries from sessions table
                final_rows = conn.execute(
                    """
                    SELECT project_name, end_time AS timestamp, summary, 'final' AS type
                    FROM sessions
                    WHERE summary IS NOT NULL AND TRIM(summary) != ''
                    ORDER BY end_time DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                # Mini summaries from events
                mini_rows = conn.execute(
                    """
                    SELECT project_name, timestamp, payload, 'mini' AS type
                    FROM events
                    WHERE source = 'agent' AND event_type = 'mini_summary'
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                summaries = []
                for r in final_rows:
                    summaries.append({
                        "project_name": r["project_name"],
                        "timestamp": r["timestamp"],
                        "summary": r["summary"],
                        "type": "final",
                    })
                for r in mini_rows:
                    try:
                        payload = json.loads(r["payload"] or "{}")
                        text = payload.get("text", "")
                    except Exception:
                        text = ""
                    summaries.append({
                        "project_name": r["project_name"],
                        "timestamp": r["timestamp"],
                        "summary": text,
                        "type": "mini",
                    })

                # Sort combined by timestamp desc, take top `limit`
                summaries.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
                summaries = summaries[:limit]

        except Exception as exc:
            self._send_error_json(503, str(exc))
            return
        self._send_json({"summaries": summaries, "count": len(summaries)})

    def _handle_settings_get(self, qs: dict) -> None:
        """Return current LLM configuration (key presence only, never the value)."""
        has_openrouter = bool(settings.OPENROUTER_API_KEY)
        has_gemini = bool(settings.GEMINI_API_KEY)
        has_ollama = bool(settings.OLLAMA_MODEL)
        provider = "none"
        if has_openrouter:
            provider = "openrouter"
        elif has_gemini:
            provider = "gemini"
        elif has_ollama:
            provider = "ollama"
        self._send_json({
            "llm_provider": settings.LLM_PROVIDER,
            "active_provider": provider,
            "has_openrouter_key": has_openrouter,
            "has_gemini_key": has_gemini,
            "ollama_model": settings.OLLAMA_MODEL,
            "ollama_base_url": settings.OLLAMA_BASE_URL,
            "dashboard_port": settings.DASHBOARD_PORT,
            "session_idle_timeout_minutes": settings.SESSION_IDLE_TIMEOUT_SECONDS // 60,
        })

    def _handle_settings_post(self, data: dict) -> None:
        """Save a new API key to ~/.contextos/.env."""
        from pathlib import Path

        env_file = settings.CONTEXTOS_HOME / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)

        lines = []
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()

        updates: dict[str, str] = {}

        key = (data.get("api_key") or "").strip()
        if key:
            env_key = "GEMINI_API_KEY" if key.startswith("AIza") else "OPENROUTER_API_KEY"
            updates[env_key] = key

        if "ollama_model" in data and data["ollama_model"]:
            updates["OLLAMA_MODEL"] = str(data["ollama_model"]).strip()

        if not updates:
            self._send_error_json(400, "No valid settings provided.")
            return

        # Apply updates to existing lines
        for env_key, value in updates.items():
            prefix = f"{env_key}="
            replaced = False
            new_lines = []
            for line in lines:
                if line.startswith(prefix):
                    new_lines.append(f"{env_key}={value}")
                    replaced = True
                else:
                    new_lines.append(line)
            if not replaced:
                new_lines.append(f"{env_key}={value}")
            lines = new_lines

        env_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        logger.info("Settings updated via dashboard: %s", list(updates.keys()))
        self._send_json({"ok": True, "updated": list(updates.keys())})


# ---------------------------------------------------------------------------
# API Server
# ---------------------------------------------------------------------------

class DashboardAPIServer:
    """
    Lightweight local HTTP server for the ContextOS dashboard.
    Runs in a daemon thread; safe to start/stop multiple times.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6543):
        self.host = host
        self.port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("DashboardAPIServer is already running.")
            return

        try:
            self._server = HTTPServer((self.host, self.port), ContextOSRequestHandler)
        except OSError as exc:
            logger.error(
                "Could not start ContextOS Dashboard API on %s:%d — %s",
                self.host, self.port, exc,
            )
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="contextos-api",
        )
        self._thread.start()
        logger.info("ContextOS Dashboard API running at %s", self.url)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            logger.info("ContextOS Dashboard API stopped.")

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
