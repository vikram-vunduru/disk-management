import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse


HOST = "127.0.0.1"
PORT = 8765
SUBSCRIPTION_CACHE_TTL_SECONDS = 300
PAYLOAD_CACHE_TTL_SECONDS = 60
REGION_SUPPORT_CACHE_TTL_SECONDS = 3600

_cache_lock = threading.Lock()
_cache = {
    "subscriptions": None,
    "payloads": {},
    "region_support": {},
}

_operations_lock = threading.Lock()
_operations = {}
OPERATION_RETENTION_SECONDS = 3600


def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


READ_ONLY_MODE = _env_bool("DISK_DASHBOARD_READONLY")
DRY_RUN_MODE = _env_bool("DISK_DASHBOARD_DRYRUN")
AUTH_TOKEN = os.environ.get("DISK_DASHBOARD_TOKEN") or None
AUDIT_DIR = os.environ.get("DISK_DASHBOARD_AUDIT_DIR") or os.path.join(
    os.path.expanduser("~"), ".disk-management", "audit"
)
try:
    MAX_MIGRATION_BATCH = max(1, int(os.environ.get("DISK_DASHBOARD_MAX_BATCH") or "25"))
except ValueError:
    MAX_MIGRATION_BATCH = 25

# State-changing az subcommand prefixes that a dry-run should never actually execute.
_STATE_CHANGING_COMMANDS = {
    ("disk", "create"),
    ("disk", "update"),
    ("disk", "delete"),
    ("snapshot", "create"),
    ("snapshot", "delete"),
    ("vm", "deallocate"),
    ("vm", "start"),
    ("vm", "update"),
}


_audit_lock = threading.Lock()


def write_audit_entry(action, subscription_id, disks, result=None, error=None, dry_run=False):
    """Append an audit record to today's JSONL file. Audit failures are swallowed
    so they never block the underlying operation."""
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(AUDIT_DIR, f"audit-{date_str}.jsonl")
        entry = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "action": action,
            "subscriptionId": subscription_id,
            "diskCount": len(disks or []),
            "disks": [
                {
                    "resourceGroup": d.get("resourceGroup"),
                    "diskName": d.get("diskName"),
                    "id": d.get("id"),
                }
                for d in (disks or [])
            ],
            "result": result,
            "error": error,
            "dryRun": dry_run,
        }
        with _audit_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        return path
    except Exception:
        return None


def is_state_changing(arguments):
    """Return True if the first two tokens of an az command modify Azure state."""
    if not arguments or len(arguments) < 2:
        return False
    return (arguments[0], arguments[1]) in _STATE_CHANGING_COMMANDS


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Azure Disk Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f8fafc;
      --bg-grad-1: #eef2ff;
      --bg-grad-2: #f8fafc;
      --paper: #ffffff;
      --paper-2: #f8fafc;
      --ink: #0f172a;
      --ink-soft: #334155;
      --muted: #64748b;
      --line: #e2e8f0;
      --line-strong: #cbd5e1;
      --accent: #6366f1;
      --accent-strong: #4f46e5;
      --accent-soft: #eef2ff;
      --success: #10b981;
      --success-soft: #d1fae5;
      --success-ink: #047857;
      --warn: #f59e0b;
      --warn-soft: #fef3c7;
      --warn-ink: #b45309;
      --danger: #ef4444;
      --danger-soft: #fee2e2;
      --danger-ink: #b91c1c;
      --shadow-sm: 0 1px 2px rgb(15 23 42 / 0.04);
      --shadow-md: 0 4px 16px -4px rgb(15 23 42 / 0.08), 0 1px 3px rgb(15 23 42 / 0.05);
      --shadow-lg: 0 12px 32px -8px rgb(99 102 241 / 0.18);
      --radius-sm: 6px;
      --radius-md: 10px;
      --radius-lg: 14px;
      --radius-xl: 20px;
    }

    * { box-sizing: border-box; }
    html, body { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", "Roboto", sans-serif;
      font-size: 14px;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at 110% -10%, rgb(99 102 241 / 0.10), transparent 60%),
        radial-gradient(900px 500px at -10% 0%, rgb(16 185 129 / 0.06), transparent 55%),
        linear-gradient(180deg, var(--bg-grad-1) 0%, var(--bg-grad-2) 220px, var(--bg) 100%);
      min-height: 100vh;
    }
    a { color: var(--accent-strong); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .shell {
      max-width: 1520px;
      margin: 0 auto;
      padding: 24px;
    }

    .hero {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 6px 4px 18px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 18px;
    }
    .hero-brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .hero-logo {
      width: 40px;
      height: 40px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent), #8b5cf6);
      display: grid;
      place-items: center;
      color: white;
      font-weight: 800;
      font-size: 18px;
      box-shadow: var(--shadow-lg);
    }
    .hero h1 {
      margin: 0;
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.015em;
    }
    .hero p {
      margin: 2px 0 0;
      color: var(--muted);
      font-size: 0.875rem;
      max-width: 720px;
      line-height: 1.45;
    }
    .hero-meta {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 0.78rem;
    }
    .hero-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent-strong);
      font-weight: 600;
      font-size: 0.75rem;
      letter-spacing: 0.02em;
    }
    .hero-pill::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 3px rgb(99 102 241 / 0.18);
    }

    .docs { margin-bottom: 18px; }
    .docs-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }
    .docs-header h2 { margin: 0 0 4px; }
    .docs-tagline { margin: 0; color: var(--muted); font-size: 0.86rem; line-height: 1.45; }
    .docs-toggle {
      background: var(--paper-2);
      border: 1px solid var(--line);
      color: var(--ink-soft);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      min-height: 30px;
      width: auto;
      cursor: pointer;
      flex-shrink: 0;
    }
    .docs-toggle:hover { background: var(--accent-soft); color: var(--accent-strong); border-color: rgb(99 102 241 / 0.25); }
    .docs-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
      margin-bottom: 16px;
    }
    .docs-section {
      background: var(--paper-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      padding: 14px 16px;
    }
    .docs-section h3 {
      margin: 0 0 10px;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-strong);
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .docs-section h3::before {
      content: "";
      width: 4px;
      height: 14px;
      background: var(--accent);
      border-radius: 2px;
    }
    .docs-section p, .docs-section li {
      font-size: 0.86rem;
      color: var(--ink-soft);
      line-height: 1.55;
    }
    .docs-section p { margin: 0 0 8px; }
    .docs-section p:last-child { margin-bottom: 0; }
    .docs-section ol, .docs-section ul {
      margin: 0;
      padding-left: 20px;
    }
    .docs-section li { margin: 5px 0; }
    .docs-section li strong { color: var(--ink); font-weight: 600; }
    .docs-section code {
      background: rgba(99, 102, 241, 0.10);
      color: var(--accent-strong);
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 0.8rem;
      font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Consolas, monospace;
    }
    .docs-section .legend-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 6px 0;
      font-size: 0.86rem;
      color: var(--ink-soft);
    }
    .docs-flags { padding: 16px 18px; }
    .flag-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      min-width: 0;
    }
    .flag-table th, .flag-table td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    .flag-table th {
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      font-weight: 600;
    }
    .flag-table td:first-child {
      font-weight: 600;
      color: var(--ink);
      white-space: nowrap;
    }
    .flag-table tr:last-child td { border-bottom: none; }
    .flag-good { color: var(--success-ink); font-weight: 600; }
    .flag-bad  { color: var(--danger-ink); font-weight: 600; }

    .layout {
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }

    .panel {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius-xl);
      padding: 20px;
      box-shadow: var(--shadow-sm);
    }

    .sidebar {
      position: sticky;
      top: 24px;
      padding: 16px;
      background: var(--paper);
    }
    .sidebar h2 {
      margin: 4px 0 4px;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 600;
    }
    .sidebar p {
      margin: 0 0 12px;
      color: var(--muted);
      line-height: 1.5;
      font-size: 0.83rem;
    }
    .nav { display: grid; gap: 4px; }
    .nav button {
      width: 100%;
      text-align: left;
      padding: 10px 12px;
      border-radius: var(--radius-md);
      background: transparent;
      border: 1px solid transparent;
      color: var(--ink-soft);
      font-weight: 600;
      font-size: 0.92rem;
      transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
      box-shadow: none;
      min-height: 0;
      display: block;
    }
    .nav button:hover {
      background: var(--paper-2);
      color: var(--ink);
      transform: none;
      box-shadow: none;
    }
    .nav button.active {
      background: var(--accent-soft);
      color: var(--accent-strong);
      border-color: rgb(99 102 241 / 0.20);
    }
    .nav button small {
      display: block;
      margin-top: 3px;
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--muted);
      letter-spacing: 0;
    }
    .nav button.active small { color: var(--accent-strong); opacity: 0.7; }

    .workspace { min-width: 0; }

    .controls { display: grid; gap: 14px; }
    .control-fields {
      display: grid;
      grid-template-columns: minmax(320px, 1.8fr) minmax(240px, 1.2fr);
      gap: 12px;
      align-items: end;
    }
    .control-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .action-group { display: contents; }
    .action-group.context-hidden { display: none; }

    .field label {
      display: block;
      margin-bottom: 6px;
      color: var(--ink-soft);
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.01em;
    }
    select, input, button {
      width: 100%;
      min-height: 38px;
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      padding: 8px 12px;
      font: inherit;
      font-size: 0.92rem;
      background: var(--paper);
      color: var(--ink);
      transition: border-color 120ms ease, box-shadow 120ms ease, background 120ms ease;
    }
    select:focus, input:focus, button:focus-visible {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgb(99 102 241 / 0.15);
    }
    button {
      cursor: pointer;
      font-weight: 600;
      width: auto;
      min-width: 0;
      white-space: nowrap;
      letter-spacing: -0.005em;
    }
    button:hover { background: var(--paper-2); }
    .primary {
      background: var(--accent);
      color: white;
      border-color: transparent;
      box-shadow: var(--shadow-sm);
    }
    .primary:hover { background: var(--accent-strong); box-shadow: var(--shadow-md); }
    .secondary { background: var(--paper); color: var(--ink); }
    .secondary:hover { background: var(--paper-2); border-color: var(--line-strong); }
    .export {
      background: var(--accent-soft);
      color: var(--accent-strong);
      border-color: rgb(99 102 241 / 0.18);
    }
    .export:hover { background: rgb(99 102 241 / 0.10); }

    .checkbox-card {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      padding: 0 12px;
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: var(--paper);
      font-weight: 600;
      font-size: 0.88rem;
      color: var(--ink-soft);
      white-space: nowrap;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .checkbox-card:hover { background: var(--paper-2); }
    .checkbox-card input {
      width: 16px;
      height: 16px;
      min-height: 16px;
      margin: 0;
      padding: 0;
      accent-color: var(--accent);
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .card {
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      padding: 14px 16px;
      transition: border-color 120ms ease, box-shadow 120ms ease;
    }
    .card:hover { border-color: var(--line-strong); }
    .card .label {
      color: var(--muted);
      font-size: 0.74rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      margin-bottom: 6px;
    }
    .card .value {
      font-size: 1.6rem;
      font-weight: 700;
      letter-spacing: -0.025em;
      color: var(--ink);
    }
    .card.accent .value { color: var(--accent-strong); }
    .card.warn .value { color: var(--warn-ink); }
    .card.danger .value { color: var(--danger-ink); }

    .meta {
      margin-top: 12px;
      color: var(--muted);
      font-size: 0.83rem;
    }

    .view-stack { display: grid; gap: 18px; margin-top: 18px; }
    .view { display: none; }
    .view.active { display: grid; gap: 18px; }

    .footer-note {
      margin-top: 24px;
      padding: 14px 18px;
      border-radius: var(--radius-lg);
      border: 1px solid var(--line);
      background: var(--paper);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 0.85rem;
    }
    .footer-copy { flex: 1 1 280px; min-width: 0; line-height: 1.45; }
    .footer-note strong { color: var(--ink); }
    .footer-links {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
      align-items: center;
      max-width: 100%;
    }
    .email-btn, .website-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-width: 0;
      min-height: 36px;
      padding: 6px 14px;
      border-radius: 999px;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.85rem;
      letter-spacing: -0.005em;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .email-btn { background: var(--ink); color: white; }
    .email-btn:hover { background: #1e293b; text-decoration: none; }
    .website-btn {
      background: var(--accent-soft);
      color: var(--accent-strong);
      border: 1px solid rgb(99 102 241 / 0.25);
    }
    .website-btn:hover { background: rgb(99 102 241 / 0.12); text-decoration: none; }

    h2 {
      margin: 0 0 14px;
      font-size: 1.05rem;
      font-weight: 700;
      letter-spacing: -0.015em;
      color: var(--ink);
    }

    .table-wrap {
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      background: var(--paper);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 920px;
    }
    th, td {
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      font-size: 0.86rem;
    }
    th {
      position: sticky;
      top: 0;
      background: var(--paper-2);
      color: var(--ink-soft);
      font-weight: 600;
      font-size: 0.74rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      z-index: 1;
      border-bottom: 1px solid var(--line);
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover { background: var(--paper-2); }

    .pill {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 600;
      letter-spacing: 0.005em;
      white-space: nowrap;
      border: 1px solid transparent;
    }
    .pill.v1     { background: var(--warn-soft); color: var(--warn-ink); }
    .pill.v2     { background: var(--success-soft); color: var(--success-ink); }
    .pill.other  { background: var(--paper-2); color: var(--muted); border-color: var(--line); }
    .pill.ok     { background: var(--success-soft); color: var(--success-ink); }
    .pill.no     { background: var(--danger-soft); color: var(--danger-ink); }
    .pill.flag-true    { background: var(--danger-soft); color: var(--danger-ink); }
    .pill.flag-false   { background: var(--success-soft); color: var(--success-ink); }
    .pill.flag-unknown { background: var(--paper-2); color: var(--muted); border-color: var(--line); }
    .pill.status-green  { background: var(--success-soft); color: var(--success-ink); border-color: rgb(16 185 129 / 0.25); }
    .pill.status-yellow { background: var(--warn-soft); color: var(--warn-ink); border-color: rgb(245 158 11 / 0.30); }
    .pill.status-red    { background: var(--danger-soft); color: var(--danger-ink); border-color: rgb(239 68 68 / 0.25); }

    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      margin-bottom: 14px;
      color: var(--muted);
      font-size: 0.83rem;
    }
    .legend .pill { font-size: 0.72rem; }

    .notice {
      margin-top: 14px;
      padding: 12px 14px;
      border-radius: var(--radius-md);
      background: var(--accent-soft);
      border: 1px solid rgb(99 102 241 / 0.20);
      color: var(--accent-strong);
      font-size: 0.86rem;
      font-weight: 500;
      display: none;
    }
    .error {
      background: var(--danger-soft);
      border-color: rgb(239 68 68 / 0.30);
      color: var(--danger-ink);
    }

    .op-log {
      margin-top: 12px;
      padding: 14px 18px;
      border-radius: var(--radius-md);
      background: #0b1020;
      color: #e2e8f0;
      font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Consolas, monospace;
      font-size: 0.78rem;
      max-height: 280px;
      overflow: auto;
      border: 1px solid #1e293b;
    }
    .op-log-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
      color: #94a3b8;
      font-size: 0.74rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
    }
    .op-log ol { margin: 0; padding-left: 0; }
    .op-log li {
      margin: 3px 0;
      list-style: none;
      padding-left: 22px;
      position: relative;
      line-height: 1.5;
    }
    .op-log li::before {
      content: "\\2022";
      position: absolute;
      left: 6px;
      color: #818cf8;
    }
    .op-log li.done::before { content: "\\2713"; color: #34d399; }
    .op-log li.err::before  { content: "\\2717"; color: #f87171; }
    .op-log .ts { color: #818cf8; margin-right: 10px; }
    .op-log .err { color: #fca5a5; }

    .icon-cell { font-size: 1.05rem; font-weight: 700; line-height: 1; }
    .icon-yes { color: var(--success); }
    .icon-no  { color: var(--danger); }
    .icon-na  { color: var(--muted); }

    .count-badge {
      display: inline-block;
      min-width: 20px;
      padding: 1px 8px;
      margin-left: 6px;
      border-radius: 999px;
      background: rgb(15 23 42 / 0.10);
      color: inherit;
      font-size: 0.74rem;
      font-weight: 700;
    }
    .primary .count-badge { background: rgb(255 255 255 / 0.22); color: white; }
    .secondary .count-badge { background: var(--paper-2); }

    button:disabled,
    button[aria-busy="true"] {
      cursor: not-allowed;
      opacity: 0.55;
      transform: none;
    }
    button[aria-busy="true"]::after {
      content: "";
      display: inline-block;
      width: 12px;
      height: 12px;
      margin-left: 8px;
      vertical-align: -2px;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .table-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .table-toolbar input {
      min-height: 36px;
      max-width: 320px;
      padding: 6px 12px 6px 32px;
      border-radius: var(--radius-md);
      font-size: 0.86rem;
      background: var(--paper) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20' fill='%2364748b'><path fill-rule='evenodd' d='M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.45 4.39l3.08 3.08a.75.75 0 11-1.06 1.06l-3.08-3.08A7 7 0 012 9z' clip-rule='evenodd'/></svg>") no-repeat 10px center / 14px 14px;
    }
    .row-count {
      color: var(--muted);
      font-size: 0.8rem;
      font-variant-numeric: tabular-nums;
    }

    .link-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      cursor: pointer;
      padding: 0;
      width: auto;
      min-height: auto;
      min-width: auto;
      font-weight: 600;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .link-btn:hover { color: #e2e8f0; background: transparent; }

    .mode-banner {
      position: sticky;
      top: 0;
      z-index: 100;
      padding: 8px 16px;
      font-size: 0.84rem;
      font-weight: 600;
      text-align: center;
      letter-spacing: 0.02em;
    }
    .mode-banner.read-only { background: var(--warn-soft); color: var(--warn-ink); border-bottom: 1px solid rgb(245 158 11 / 0.30); }
    .mode-banner.dry-run   { background: var(--accent-soft); color: var(--accent-strong); border-bottom: 1px solid rgb(99 102 241 / 0.30); }

    .modal-overlay {
      position: fixed;
      inset: 0;
      background: rgb(15 23 42 / 0.55);
      backdrop-filter: blur(4px);
      display: grid;
      place-items: center;
      z-index: 1000;
    }
    .modal-overlay[hidden] { display: none; }
    .modal {
      background: var(--paper);
      border-radius: var(--radius-xl);
      max-width: 640px;
      width: calc(100% - 32px);
      max-height: calc(100vh - 64px);
      display: flex;
      flex-direction: column;
      box-shadow: 0 24px 48px -12px rgb(15 23 42 / 0.40);
      overflow: hidden;
      border: 1px solid var(--line);
    }
    .modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 22px 12px;
      border-bottom: 1px solid var(--line);
    }
    .modal-header h3 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: -0.015em;
    }
    .modal-header.danger h3 { color: var(--danger-ink); }
    .modal-close {
      background: transparent;
      border: none;
      font-size: 1.3rem;
      line-height: 1;
      color: var(--muted);
      cursor: pointer;
      padding: 4px 8px;
      width: auto;
      min-height: auto;
      min-width: auto;
    }
    .modal-close:hover { color: var(--ink); background: var(--paper-2); }
    .modal-body {
      padding: 16px 22px;
      overflow: auto;
      font-size: 0.9rem;
      line-height: 1.55;
      color: var(--ink-soft);
    }
    .modal-body p { margin: 0 0 10px; }
    .modal-body ul, .modal-body ol { margin: 0 0 10px; padding-left: 22px; }
    .modal-body li { margin: 3px 0; }
    .modal-body strong { color: var(--ink); }
    .modal-impact {
      background: var(--paper-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      padding: 10px 12px;
      max-height: 180px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, "Cascadia Code", Consolas, monospace;
      font-size: 0.78rem;
      color: var(--ink);
      margin: 6px 0 12px;
    }
    .modal-impact ul { margin: 0; padding-left: 18px; list-style: none; }
    .modal-impact li { padding: 2px 0; }
    .modal-impact li::before { content: "→ "; color: var(--muted); }
    .modal-footer {
      padding: 14px 22px 18px;
      border-top: 1px solid var(--line);
      background: var(--paper-2);
    }
    .modal-confirm-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
      font-size: 0.86rem;
      color: var(--ink-soft);
    }
    .modal-confirm-row input {
      flex: 1;
      min-height: 36px;
      font-family: ui-monospace, monospace;
      letter-spacing: 0.04em;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }
    .modal-actions button { min-width: 100px; }
    .modal.danger .modal-actions .primary {
      background: var(--danger);
      border-color: var(--danger);
    }
    .modal.danger .modal-actions .primary:hover { background: var(--danger-ink); }

    body.disclaimer-pending .workspace { pointer-events: none; opacity: 0.4; }

    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      .sidebar { position: static; }
      .control-fields { grid-template-columns: 1fr; }
      .control-actions { flex-wrap: wrap; }
      .summary { grid-template-columns: repeat(2, 1fr); }
      .hero { flex-direction: column; align-items: flex-start; }
    }
    @media (max-width: 700px) {
      .shell { padding: 16px; }
      .summary { grid-template-columns: 1fr; }
      .hero h1 { font-size: 1.2rem; }
      .footer-note { flex-direction: column; align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div id="modeBanner" class="mode-banner" hidden></div>

  <div id="modalOverlay" class="modal-overlay" hidden>
    <div class="modal" id="modalEl" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <div class="modal-header" id="modalHeader">
        <h3 id="modalTitle">Confirm</h3>
        <button class="modal-close" id="modalCloseBtn" type="button" aria-label="Close">&times;</button>
      </div>
      <div class="modal-body" id="modalBody"></div>
      <div class="modal-footer">
        <div class="modal-confirm-row" id="modalConfirmRow" hidden>
          <label for="modalConfirmInput">Type <strong id="modalConfirmWord">YES</strong> to confirm:</label>
          <input type="text" id="modalConfirmInput" autocomplete="off" autocapitalize="characters" spellcheck="false">
        </div>
        <div class="modal-actions">
          <button id="modalCancelBtn" class="secondary" type="button">Cancel</button>
          <button id="modalOkBtn" class="primary" type="button">Confirm</button>
        </div>
      </div>
    </div>
  </div>

  <div class="shell">
    <header class="hero">
      <div class="hero-brand">
        <div class="hero-logo" aria-hidden="true">AZ</div>
        <div>
          <h1>Azure Disk Dashboard</h1>
          <p>Inventory, V2 readiness, snapshots, and unattached cleanup &mdash; one workspace.</p>
        </div>
      </div>
      <div class="hero-meta">
        <span class="hero-pill">Premium SSD v2 readiness</span>
      </div>
    </header>

    <div class="layout">
      <aside class="panel sidebar">
        <h2>Views</h2>
        <div class="nav">
          <button id="navDocs" class="active">Documentation<small>About this tool, flags, caveats</small></button>
          <button id="navAll">All Disks<small>Full managed disk inventory</small></button>
          <button id="navEligible">Premium LRS Data Disks<small>V2 readiness with attribute flags</small></button>
          <button id="navUnattached">Unattached Disks<small>Selection and cleanup</small></button>
        </div>
      </aside>

      <section class="workspace">
        <section class="panel" id="controlsPanel">
          <div class="controls">
            <div class="control-fields">
              <div class="field">
                <label for="subscription">Subscription</label>
                <select id="subscription"></select>
              </div>
              <div class="field">
                <label for="resourceGroup">Resource Group</label>
                <input id="resourceGroup" type="text" placeholder="Optional filter">
              </div>
            </div>
            <div class="control-actions">
              <div class="action-group" data-context="all">
                <button id="loadBtn" class="primary">Load Disks</button>
                <button id="csvBtn" class="export">Export CSV</button>
              </div>
              <div class="action-group" data-context="eligible">
                <label class="checkbox-card"><input id="backupBeforeMigration" type="checkbox"> Backup Before Migration</label>
                <button id="backupSelectedBtn" class="secondary">Backup Selected<span class="count-badge" id="backupCount">0</span></button>
                <button id="migrateSelectedBtn" class="secondary">Migrate Selected<span class="count-badge" id="migrateCount">0</span></button>
              </div>
              <div class="action-group" data-context="unattached">
                <button id="deleteSelectedBtn" class="secondary">Delete Selected<span class="count-badge" id="deleteCount">0</span></button>
              </div>
            </div>
          </div>
          <div id="notice" class="notice"></div>
          <div id="opLog" class="op-log" hidden>
            <div class="op-log-header">
              <strong>Activity log</strong>
              <button id="opLogClose" type="button" class="link-btn">Hide</button>
            </div>
            <ol id="opLogList"></ol>
          </div>
          <div class="summary" id="summary" hidden>
            <div class="card"><div class="label">Total Disks</div><div class="value" id="totalDisks">0</div></div>
            <div class="card warn"><div class="label">V1 Disks</div><div class="value" id="v1Disks">0</div></div>
            <div class="card accent"><div class="label">V2 Disks</div><div class="value" id="v2Disks">0</div></div>
            <div class="card"><div class="label">V2 Ready (green)</div><div class="value" id="eligibleDisks">0</div></div>
            <div class="card danger"><div class="label">Unattached Disks</div><div class="value" id="unattachedDisks">0</div></div>
          </div>
          <div id="meta" class="meta"></div>
        </section>

        <section class="view-stack" id="content">
          <div id="viewDocs" class="view active">
            <div class="panel docs">
              <div class="docs-header">
                <div>
                  <h2>About this dashboard</h2>
                  <p class="docs-tagline">A reference for what this tool does, how to navigate it, and how to act on its findings.</p>
                </div>
              </div>
              <div class="docs-section" style="border-left: 4px solid var(--warn); background: var(--warn-soft); margin-bottom: 16px;">
                <h3 style="color: var(--warn-ink);">Disclaimer &amp; safe use</h3>
                <p><strong>This tool is provided "AS IS" without warranty of any kind.</strong> The author and contributors accept no liability for any data loss, service interruption, downtime, or financial impact arising from the use of, or inability to use, this software.</p>
                <p>By initiating <strong>Migrate</strong>, <strong>Backup</strong>, or <strong>Delete</strong> actions you acknowledge that:</p>
                <ul>
                  <li>You have the appropriate authorization to perform these actions on the target Azure resources.</li>
                  <li>You have verified the selected scope and confirmed the impact with the affected workload owners.</li>
                  <li>You have taken independent backups (snapshots, Azure Backup, or equivalent) before destructive operations.</li>
                  <li>The displayed eligibility status is a best-effort evaluation and may not capture every runtime constraint Azure enforces (region capacity, RBAC, ASR replication state, etc.).</li>
                  <li>Migration and deletion actions are <strong>irreversible without prior backups</strong>.</li>
                </ul>
                <p>Always review the disk list and the Activity Log before, during, and after every action. When in doubt, dry-run by reading the inventory view first.</p>
              </div>

              <div class="docs-grid">
                <article class="docs-section">
                  <h3>What it is</h3>
                  <p>An interactive dashboard over the Azure CLI for inspecting managed disks across a subscription, evaluating their readiness for migration from <strong>Premium SSD v1</strong> (<code>Premium_LRS</code>) to <strong>Premium SSD v2</strong> (<code>PremiumV2_LRS</code>), creating snapshot backups, and cleaning up unattached disks &mdash; all from one page.</p>
                  <p>The server is a single Python file (<code>dashboard/app.py</code>) that shells out to <code>az</code>. Nothing is stored persistently; results are cached in memory for 60 seconds (inventory) and 1 hour (region support).</p>
                </article>

                <article class="docs-section">
                  <h3>Quick start</h3>
                  <ol>
                    <li>Install Azure CLI and run <code>az login</code>.</li>
                    <li>Start the server: <code>python dashboard/app.py</code></li>
                    <li>Open <code>http://127.0.0.1:8765</code>.</li>
                    <li>Pick a subscription, optionally type a resource group filter, and click <strong>Load Disks</strong>.</li>
                    <li>Switch tabs in the sidebar to inspect each view.</li>
                  </ol>
                </article>

                <article class="docs-section">
                  <h3>Three data views</h3>
                  <ul>
                    <li><strong>All Disks</strong> &mdash; full inventory with version, SKU, attachment, OS-disk flag, caching, region.</li>
                    <li><strong>Premium LRS Data Disks</strong> &mdash; only Premium_LRS data disks, with V2-readiness flags (a&ndash;f) and a colored status.</li>
                    <li><strong>Unattached Disks</strong> &mdash; selection-driven cleanup workflow.</li>
                  </ul>
                </article>

                <article class="docs-section">
                  <h3>Actions</h3>
                  <ul>
                    <li><strong>Load Disks</strong> &mdash; queries Azure for VMs, disks, and PremiumV2 region support.</li>
                    <li><strong>Export CSV</strong> &mdash; downloads the full inventory (or selected unattached disks).</li>
                    <li><strong>Backup Selected</strong> &mdash; creates snapshots for selected green disks without migrating.</li>
                    <li><strong>Backup Before Migration</strong> &mdash; checkbox; if ticked, snapshot is taken before SKU change.</li>
                    <li><strong>Migrate Selected</strong> &mdash; deallocates the VM (if attached), runs <code>az disk update --sku PremiumV2_LRS</code>, and starts the VM.</li>
                    <li><strong>Delete Selected</strong> &mdash; deletes selected unattached disks after a confirm prompt.</li>
                  </ul>
                </article>

                <article class="docs-section">
                  <h3>Status colors</h3>
                  <div class="legend-row"><span class="pill status-green">Green</span> Region supports v2 and no blockers &mdash; ready for direct conversion.</div>
                  <div class="legend-row"><span class="pill status-yellow">Yellow</span> Region supports v2, but at least one workaround is needed.</div>
                  <div class="legend-row"><span class="pill status-red">Red</span> Region does not advertise PremiumV2_LRS; v2 not possible there.</div>
                  <p>Only <strong>green</strong> disks are selectable for migration. Yellow disks need the listed workaround applied first; red disks need to be moved to a supported region.</p>
                </article>

                <article class="docs-section">
                  <h3>Productivity</h3>
                  <ul>
                    <li><strong>Search box</strong> per table filters rows by any visible text (disk name, RG, VM, SKU, status&hellip;).</li>
                    <li><strong>Selection counts</strong> on action buttons; buttons disable when nothing is selected.</li>
                    <li><strong>Activity log</strong> &mdash; live-updating panel below the action bar; ✓ for completed steps, ✗ for errors.</li>
                    <li><strong>Contextual buttons</strong> &mdash; only the actions relevant to the active tab are shown.</li>
                    <li><strong>Caching</strong> &mdash; inventory 60s, region support 1h; click <strong>Load Disks</strong> again to force a refresh.</li>
                  </ul>
                </article>
              </div>

              <div class="docs-section docs-flags">
                <h3>Eligibility flags (a&ndash;f)</h3>
                <div style="overflow:auto;">
                  <table class="flag-table">
                    <thead>
                      <tr>
                        <th>Flag</th>
                        <th>What it checks</th>
                        <th>True means</th>
                        <th>Workaround if blocked</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td>a. Region Supported</td>
                        <td>Whether <code>PremiumV2_LRS</code> is available in the disk's region (per <code>az vm list-skus</code>).</td>
                        <td><span class="flag-good">Good</span> &mdash; region supports v2.</td>
                        <td>Move workload to a v2-enabled region.</td>
                      </tr>
                      <tr>
                        <td>b. Sector &ne; 512</td>
                        <td>Disk's logical sector size. v2 in-place conversion requires 512.</td>
                        <td><span class="flag-bad">Issue</span> &mdash; disk uses 4096-byte sectors.</td>
                        <td>Snapshot &rarr; create new V2 disk with sector 512 &rarr; swap.</td>
                      </tr>
                      <tr>
                        <td>c. Host Caching On</td>
                        <td>Whether the VM-side caching for this disk is ReadOnly or ReadWrite.</td>
                        <td><span class="flag-bad">Issue</span> &mdash; caching is enabled.</td>
                        <td>Set caching to <code>None</code> on the VM's disk attachment.</td>
                      </tr>
                      <tr>
                        <td>d. Bursting On</td>
                        <td>On-demand bursting (<code>burstingEnabled = true</code>) on the disk.</td>
                        <td><span class="flag-bad">Issue</span> &mdash; bursting is enabled.</td>
                        <td>Disable bursting before converting.</td>
                      </tr>
                      <tr>
                        <td>e. Double Encryption On</td>
                        <td>Disk uses <code>EncryptionAtRestWithPlatformAndCustomerKeys</code>.</td>
                        <td><span class="flag-bad">Issue</span> &mdash; double encryption enabled.</td>
                        <td>Re-key with a single-key DES, or recreate from snapshot.</td>
                      </tr>
                      <tr>
                        <td>f. ASR Enabled</td>
                        <td>Heuristic match on disk tags or name suggesting Azure Site Recovery replication.</td>
                        <td><span class="flag-bad">Issue</span> &mdash; ASR appears active.</td>
                        <td>Disable ASR replication for the disk before converting.</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>

              <div class="docs-grid">
                <article class="docs-section">
                  <h3>Caveats</h3>
                  <ul>
                    <li><strong>Zonal requirement.</strong> In-place <code>Premium_LRS &rarr; PremiumV2_LRS</code> works only when the source disk is zonal. Regional disks need snapshot &amp; recreate.</li>
                    <li><strong>ASR detection is heuristic.</strong> The dashboard scans tags and disk names for ASR markers. Authoritative ASR state requires a Recovery Services Vault query.</li>
                    <li><strong>Sector size returned as null.</strong> <code>az disk list</code> often omits <code>logicalSectorSize</code>; the dashboard treats null as 512 (the default).</li>
                    <li><strong>OS disks excluded.</strong> The Premium LRS Data Disks view filters out OS disks; OS-to-v2 conversion is not supported.</li>
                    <li><strong>Migrate deallocates the VM.</strong> Attached-disk migration deallocates the VM, updates the SKU, and restarts the VM. Plan downtime.</li>
                  </ul>
                </article>

                <article class="docs-section">
                  <h3>Permissions</h3>
                  <p>The dashboard uses your Azure CLI login. Roles needed:</p>
                  <ul>
                    <li><strong>Reader</strong> on the subscription &mdash; to load inventory.</li>
                    <li><strong>Disk Contributor</strong> &mdash; for snapshot / migrate / delete.</li>
                    <li><strong>Virtual Machine Contributor</strong> &mdash; for the VM deallocate / start during attached-disk migration.</li>
                  </ul>
                </article>

                <article class="docs-section">
                  <h3>Tips</h3>
                  <ul>
                    <li>Tick <strong>Backup Before Migration</strong> for a safety snapshot &mdash; rollback is just an <code>az disk create --source &lt;snapshot&gt;</code>.</li>
                    <li>If a load takes a while, <code>az vm list-skus</code> is the slow part; it caches for an hour after first success.</li>
                    <li>Use the search box to type a disk name when working in subscriptions with thousands of disks.</li>
                  </ul>
                </article>
              </div>
            </div>
          </div>

          <div id="viewAll" class="view">
            <div class="panel">
              <h2>All Disks</h2>
              <div class="table-toolbar">
                <input id="searchInventory" type="search" placeholder="Filter by disk, RG, VM, region, SKU...">
                <span class="row-count" id="inventoryRowCount"></span>
              </div>
              <div class="table-wrap"><table id="inventoryTable"></table></div>
            </div>
          </div>

          <div id="viewEligible" class="view">
            <div class="panel">
              <h2>Premium SSD LRS Data Disks</h2>
              <div class="legend">
                <span><span class="pill status-green">Green</span> Region supported &amp; no blockers</span>
                <span><span class="pill status-yellow">Yellow</span> Region supported, workarounds needed</span>
                <span><span class="pill status-red">Red</span> Region does not support Premium SSD v2</span>
              </div>
              <div class="table-toolbar">
                <input id="searchEligible" type="search" placeholder="Filter by disk, RG, VM, region, status...">
                <span class="row-count" id="eligibleRowCount"></span>
              </div>
              <div id="migrationMeta" class="meta"></div>
              <div class="table-wrap"><table id="eligibleTable"></table></div>
            </div>
          </div>

          <div id="viewUnattached" class="view">
            <div class="panel">
              <h2>Unattached Disks</h2>
              <div class="table-toolbar">
                <input id="searchUnattached" type="search" placeholder="Filter by disk, RG, region, SKU...">
                <span class="row-count" id="unattachedRowCount"></span>
              </div>
              <div id="unattachedMeta" class="meta"></div>
              <div class="table-wrap"><table id="unattachedTable"></table></div>
            </div>
          </div>
        </section>

        <div class="footer-note">
          <div class="footer-copy">
            Author: <strong>Vikram Vunduru</strong><br>
            More work at <a href="https://www.vikramvunduru.com" target="_blank" rel="noopener noreferrer">vikramvunduru.com</a>. For support, email <a href="mailto:vikram.vunduru@gmail.com">vikram.vunduru@gmail.com</a> or <a href="mailto:hi@vikramvunduru.com">hi@vikramvunduru.com</a>.
          </div>
          <div class="footer-links">
            <a class="website-btn" href="https://www.vikramvunduru.com" target="_blank" rel="noopener noreferrer">
              <span aria-hidden="true">&#x25C6;</span>
              <span>Portfolio</span>
            </a>
            <a class="email-btn" href="mailto:vikram.vunduru@gmail.com">
              <span aria-hidden="true">&#x2709;</span>
              <span>vikram.vunduru@gmail.com</span>
            </a>
            <a class="email-btn" href="mailto:hi@vikramvunduru.com">
              <span aria-hidden="true">&#x2709;</span>
              <span>hi@vikramvunduru.com</span>
            </a>
          </div>
        </div>
      </section>
    </div>
  </div>

  <script>
    const state = {
      data: null,
      subscriptions: [],
      selectedUnattachedDiskIds: new Set(),
      selectedMigrationDiskIds: new Set()
    };

    const noticeEl = document.getElementById("notice");
    const summaryEl = document.getElementById("summary");
    const contentEl = document.getElementById("content");
    const metaEl = document.getElementById("meta");
    const unattachedMetaEl = document.getElementById("unattachedMeta");
    const migrationMetaEl = document.getElementById("migrationMeta");
    const navButtons = {
      docs: document.getElementById("navDocs"),
      all: document.getElementById("navAll"),
      eligible: document.getElementById("navEligible"),
      unattached: document.getElementById("navUnattached")
    };
    const viewPanels = {
      docs: document.getElementById("viewDocs"),
      all: document.getElementById("viewAll"),
      eligible: document.getElementById("viewEligible"),
      unattached: document.getElementById("viewUnattached")
    };
    const controlsPanelEl = document.getElementById("controlsPanel");

    function showNotice(message, isError = false) {
      noticeEl.textContent = message;
      noticeEl.className = isError ? "notice error" : "notice";
      noticeEl.style.display = "block";
    }

    function clearNotice() {
      noticeEl.style.display = "none";
      noticeEl.textContent = "";
      noticeEl.className = "notice";
    }

    async function fetchJson(url) {
      const headers = {};
      const token = localStorage.getItem("dashboard-token");
      if (token) headers["X-Dashboard-Token"] = token;
      const response = await fetch(url, { headers });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Request failed");
      }
      return payload;
    }

    // -------- modal --------
    const modalOverlay = document.getElementById("modalOverlay");
    const modalEl = document.getElementById("modalEl");
    const modalHeader = document.getElementById("modalHeader");
    const modalTitle = document.getElementById("modalTitle");
    const modalBody = document.getElementById("modalBody");
    const modalConfirmRow = document.getElementById("modalConfirmRow");
    const modalConfirmWord = document.getElementById("modalConfirmWord");
    const modalConfirmInput = document.getElementById("modalConfirmInput");
    const modalOkBtn = document.getElementById("modalOkBtn");
    const modalCancelBtn = document.getElementById("modalCancelBtn");
    const modalCloseBtn = document.getElementById("modalCloseBtn");

    function showModal({ title, bodyHtml, confirmWord = null, okLabel = "Confirm", cancelLabel = "Cancel", danger = false, dismissible = true }) {
      return new Promise((resolve) => {
        modalTitle.textContent = title;
        modalBody.innerHTML = bodyHtml;
        modalOkBtn.textContent = okLabel;
        modalCancelBtn.textContent = cancelLabel;
        modalEl.classList.toggle("danger", danger);
        modalHeader.classList.toggle("danger", danger);
        modalCloseBtn.hidden = !dismissible;
        if (confirmWord) {
          modalConfirmRow.hidden = false;
          modalConfirmWord.textContent = confirmWord;
          modalConfirmInput.value = "";
          modalOkBtn.disabled = true;
          const onInput = () => { modalOkBtn.disabled = modalConfirmInput.value !== confirmWord; };
          modalConfirmInput.addEventListener("input", onInput);
          modalConfirmInput._dispose = () => modalConfirmInput.removeEventListener("input", onInput);
        } else {
          modalConfirmRow.hidden = true;
          modalOkBtn.disabled = false;
        }
        const cleanup = () => {
          modalOverlay.hidden = true;
          modalOkBtn.removeEventListener("click", onOk);
          modalCancelBtn.removeEventListener("click", onCancel);
          modalCloseBtn.removeEventListener("click", onCancel);
          document.removeEventListener("keydown", onKey);
          if (modalConfirmInput._dispose) { modalConfirmInput._dispose(); modalConfirmInput._dispose = null; }
        };
        const onOk = () => { cleanup(); resolve(true); };
        const onCancel = () => { cleanup(); resolve(false); };
        const onKey = (ev) => {
          if (ev.key === "Escape" && dismissible) onCancel();
          if (ev.key === "Enter" && !modalOkBtn.disabled) onOk();
        };
        modalOkBtn.addEventListener("click", onOk);
        modalCancelBtn.addEventListener("click", onCancel);
        modalCloseBtn.addEventListener("click", onCancel);
        document.addEventListener("keydown", onKey);
        modalOverlay.hidden = false;
        if (confirmWord) modalConfirmInput.focus();
        else modalOkBtn.focus();
      });
    }

    function escapeHtml(text) {
      return String(text == null ? "" : text).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
    }

    async function postJson(url, payload) {
      const headers = { "Content-Type": "application/json" };
      const token = localStorage.getItem("dashboard-token");
      if (token) headers["X-Dashboard-Token"] = token;
      const response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        const err = new Error(data.error || "Request failed");
        err.log = data.log || [];
        throw err;
      }
      return data;
    }

    const opLogEl = document.getElementById("opLog");
    const opLogListEl = document.getElementById("opLogList");
    document.getElementById("opLogClose").addEventListener("click", () => { opLogEl.hidden = true; });

    function renderOpLog(entries) {
      if (!entries || entries.length === 0) {
        opLogEl.hidden = true;
        opLogListEl.innerHTML = "";
        return;
      }
      const completionPatterns = [/^Done\b/i, /^Migrated /, /^Snapshot .* created/i, /^Deleted /, /^VM .* (deallocated|started)/i, /complete/i];
      opLogListEl.innerHTML = entries.map(entry => {
        const text = entry.message || "";
        const isError = text.startsWith("ERROR");
        const isDone  = !isError && completionPatterns.some(rx => rx.test(text));
        const liCls   = isError ? "err" : (isDone ? "done" : "");
        const txtCls  = isError ? "err" : "";
        const safe    = text.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
        return `<li class="${liCls}"><span class="ts">${entry.ts || ""}</span><span class="${txtCls}">${safe}</span></li>`;
      }).join("");
      opLogEl.hidden = false;
      opLogEl.scrollTop = opLogEl.scrollHeight;
    }

    async function pollOperation(opId) {
      while (true) {
        const status = await fetchJson(`/api/op-status/${opId}`);
        renderOpLog(status.log);
        if (status.status === "complete") {
          return { ok: true, result: status.result, log: status.log };
        }
        if (status.status === "failed") {
          return { ok: false, error: status.error, log: status.log };
        }
        await new Promise(r => setTimeout(r, 1000));
      }
    }

    async function withBusy(button, fn) {
      const wasDisabled = button.disabled;
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      try { return await fn(); }
      finally {
        button.removeAttribute("aria-busy");
        button.disabled = wasDisabled;
      }
    }

    function subscriptionLabel(subscription) {
      return `${subscription.name} (${subscription.id})`;
    }

    async function loadSubscriptions() {
      try {
        const payload = await fetchJson("/api/subscriptions");
        state.subscriptions = payload.subscriptions || [];
        const select = document.getElementById("subscription");
        select.innerHTML = "";
        for (const subscription of state.subscriptions) {
          const option = document.createElement("option");
          option.value = subscription.id;
          option.textContent = subscriptionLabel(subscription);
          select.appendChild(option);
        }
        clearNotice();
      } catch (error) {
        showNotice(error.message, true);
      }
    }

    function pill(value, kind) {
      return `<span class="pill ${kind}">${value}</span>`;
    }

    function flagIcon(value) {
      if (value === null || value === undefined) {
        return `<span class="icon-cell icon-na" title="Unknown">?</span>`;
      }
      if (value === true) {
        return `<span class="icon-cell icon-yes" title="Yes">✓</span>`;
      }
      return `<span class="icon-cell icon-no" title="No">✗</span>`;
    }

    // For "issue" flags (b-f): TRUE means problem (red), FALSE means clean (green)
    function issueIcon(value) {
      if (value === null || value === undefined) {
        return `<span class="icon-cell icon-na" title="Unknown">?</span>`;
      }
      if (value === true) {
        return `<span class="icon-cell icon-no" title="Yes (issue)">⚠</span>`;
      }
      return `<span class="icon-cell icon-yes" title="No">✓</span>`;
    }

    // For positive flags (a Region Supported): TRUE = green
    function positiveIcon(value) {
      if (value === null || value === undefined) {
        return `<span class="icon-cell icon-na" title="Unknown">?</span>`;
      }
      if (value === true) {
        return `<span class="icon-cell icon-yes" title="Supported">✓</span>`;
      }
      return `<span class="icon-cell icon-no" title="Not supported">✗</span>`;
    }

    function portalLink(url, label = "Open") {
      if (!url) {
        return "";
      }
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    }

    function setActiveView(viewName) {
      for (const [key, button] of Object.entries(navButtons)) {
        button.classList.toggle("active", key === viewName);
      }
      for (const [key, panel] of Object.entries(viewPanels)) {
        panel.classList.toggle("active", key === viewName);
      }
      controlsPanelEl.hidden = viewName === "docs";
      const showByContext = { docs: [], all: ["all"], eligible: ["all","eligible"], unattached: ["all","unattached"] };
      const allowed = new Set(showByContext[viewName] || ["all"]);
      for (const group of document.querySelectorAll(".action-group")) {
        const ctx = group.dataset.context || "all";
        group.classList.toggle("context-hidden", !allowed.has(ctx));
      }
    }

    function updateSelectionCounts() {
      const migrate = state.selectedMigrationDiskIds.size;
      const unattached = state.selectedUnattachedDiskIds.size;
      document.getElementById("backupCount").textContent = migrate;
      document.getElementById("migrateCount").textContent = migrate;
      document.getElementById("deleteCount").textContent = unattached;
      document.getElementById("backupSelectedBtn").disabled = migrate === 0;
      document.getElementById("migrateSelectedBtn").disabled = migrate === 0;
      document.getElementById("deleteSelectedBtn").disabled = unattached === 0;
    }

    function applyTableFilter(tableId, query) {
      const table = document.getElementById(tableId);
      if (!table) return 0;
      const tbody = table.querySelector("tbody");
      if (!tbody) return 0;
      const q = (query || "").trim().toLowerCase();
      let visible = 0;
      for (const row of tbody.rows) {
        const text = row.textContent.toLowerCase();
        const match = !q || text.includes(q);
        row.style.display = match ? "" : "none";
        if (match) visible++;
      }
      return visible;
    }

    function refreshAllFilters() {
      const inv = document.getElementById("searchInventory");
      const elig = document.getElementById("searchEligible");
      const una = document.getElementById("searchUnattached");
      const invCount = applyTableFilter("inventoryTable", inv.value);
      const eligCount = applyTableFilter("eligibleTable", elig.value);
      const unaCount = applyTableFilter("unattachedTable", una.value);
      const invTotal = state.data ? state.data.inventory.length : 0;
      const eligTotal = state.data ? (state.data.premiumLrsDataDisks || []).length : 0;
      const unaTotal = state.data ? state.data.unattached.length : 0;
      document.getElementById("inventoryRowCount").textContent = `${invCount} of ${invTotal} disks`;
      document.getElementById("eligibleRowCount").textContent = `${eligCount} of ${eligTotal} disks`;
      document.getElementById("unattachedRowCount").textContent = `${unaCount} of ${unaTotal} disks`;
    }

    function renderTable(elementId, columns, rows, formatter = null) {
      const table = document.getElementById(elementId);
      const head = `<thead><tr>${columns.map(column => `<th>${column.label}</th>`).join("")}</tr></thead>`;
      const bodyRows = rows.length === 0
        ? `<tr><td colspan="${columns.length}">No rows</td></tr>`
        : rows.map(row => {
            const cells = columns.map(column => {
              const value = formatter ? formatter(column.key, row[column.key], row) : row[column.key];
              return `<td>${value ?? ""}</td>`;
            }).join("");
            return `<tr>${cells}</tr>`;
          }).join("");
      table.innerHTML = `${head}<tbody>${bodyRows}</tbody>`;
    }

    function renderData(data) {
      state.data = data;
      const availableUnattachedIds = new Set((data.unattached || []).map(item => item.id));
      const availableMigrationIds = new Set((data.premiumLrsDataDisks || []).filter(item => item.status === "green").map(item => item.id));
      state.selectedUnattachedDiskIds = new Set(
        [...state.selectedUnattachedDiskIds].filter(id => availableUnattachedIds.has(id))
      );
      state.selectedMigrationDiskIds = new Set(
        [...state.selectedMigrationDiskIds].filter(id => availableMigrationIds.has(id))
      );
      summaryEl.hidden = false;
      contentEl.hidden = false;

      document.getElementById("totalDisks").textContent = data.summary.totalDisks;
      document.getElementById("v1Disks").textContent = data.summary.v1Disks;
      document.getElementById("v2Disks").textContent = data.summary.v2Disks;
      document.getElementById("eligibleDisks").textContent = data.summary.eligibleDisks;
      document.getElementById("unattachedDisks").textContent = data.summary.unattachedDisks;

      const selectedSubscription = state.subscriptions.find(item => item.id === data.subscriptionId);
      const scope = data.resourceGroupName ? `Resource Group: ${data.resourceGroupName}` : "All resource groups";
      const cacheState = data.cacheHit ? "cache" : "live";
      const regionCheck = data.regionSupportChecked ? "region-check on" : "region-check skipped";
      metaEl.textContent = `Loaded ${selectedSubscription ? selectedSubscription.name : data.subscriptionId} | ${scope} | Generated ${data.generatedAt} | ${cacheState} | ${regionCheck} | ${data.durationMs} ms`;

      renderTable(
        "inventoryTable",
        [
          { key: "resourceGroup", label: "Resource Group" },
          { key: "diskName", label: "Disk" },
          { key: "diskVersion", label: "Version" },
          { key: "sku", label: "SKU" },
          { key: "attached", label: "Attached" },
          { key: "vmName", label: "VM" },
          { key: "isOsDisk", label: "OS Disk" },
          { key: "caching", label: "Caching" },
          { key: "location", label: "Region" }
        ],
        data.inventory,
        (key, value) => {
          if (key === "diskVersion") {
            const kind = value === "V1" ? "v1" : value === "V2" ? "v2" : "other";
            return pill(value, kind);
          }
          if (key === "attached" || key === "isOsDisk") {
            return pill(value ? "Yes" : "No", value ? "ok" : "no");
          }
          return value ?? "";
        }
      );

      const premiumLrsRows = data.premiumLrsDataDisks || [];
      renderTable(
        "eligibleTable",
        [
          { key: "select", label: "Select" },
          { key: "status", label: "Status" },
          { key: "resourceGroup", label: "Resource Group" },
          { key: "vmName", label: "VM" },
          { key: "diskName", label: "Disk" },
          { key: "location", label: "Region" },
          { key: "regionSupported", label: "a. Region Supported" },
          { key: "sectorNot512", label: "b. Sector ≠ 512" },
          { key: "hostCachingEnabled", label: "c. Host Caching On" },
          { key: "burstingEnabled", label: "d. Bursting On" },
          { key: "doubleEncryptionEnabled", label: "e. Double Encryption On" },
          { key: "asrEnabled", label: "f. ASR Enabled" },
          { key: "notes", label: "Notes" },
          { key: "portalUrl", label: "Portal" }
        ],
        premiumLrsRows,
        (key, value, row) => {
          if (key === "select") {
            if (row.status !== "green") {
              return "";
            }
            const checked = state.selectedMigrationDiskIds.has(row.id) ? "checked" : "";
            return `<input type="checkbox" class="migration-select" data-disk-id="${row.id}" ${checked}>`;
          }
          if (key === "status") {
            const label = row.status === "green" ? "Green" : row.status === "yellow" ? "Yellow" : "Red";
            return pill(label, `status-${row.status}`);
          }
          if (key === "regionSupported") {
            return positiveIcon(value);
          }
          if (["sectorNot512","hostCachingEnabled","burstingEnabled","doubleEncryptionEnabled","asrEnabled"].includes(key)) {
            return issueIcon(value);
          }
          if (key === "portalUrl") {
            return portalLink(value);
          }
          return value ?? "";
        }
      );
      const greenCount = premiumLrsRows.filter(item => item.status === "green").length;
      const yellowCount = premiumLrsRows.filter(item => item.status === "yellow").length;
      const redCount = premiumLrsRows.filter(item => item.status === "red").length;
      migrationMetaEl.textContent = `${state.selectedMigrationDiskIds.size} of ${greenCount} green disks selected | Green: ${greenCount} | Yellow: ${yellowCount} | Red: ${redCount} | Total Premium LRS data disks: ${premiumLrsRows.length}`;
      bindMigrationSelectionHandlers();

      renderTable(
        "unattachedTable",
        [
          { key: "select", label: "Select" },
          { key: "resourceGroup", label: "Resource Group" },
          { key: "diskName", label: "Disk" },
          { key: "sku", label: "SKU" },
          { key: "diskState", label: "Disk State" },
          { key: "location", label: "Region" },
          { key: "portalUrl", label: "Portal" }
        ],
        data.unattached,
        (key, value, row) => {
          if (key === "select") {
            const checked = state.selectedUnattachedDiskIds.has(row.id) ? "checked" : "";
            return `<input type="checkbox" class="unattached-select" data-disk-id="${row.id}" ${checked}>`;
          }
          if (key === "portalUrl") {
            return portalLink(value);
          }
          return value ?? "";
        }
      );
      unattachedMetaEl.textContent = `${state.selectedUnattachedDiskIds.size} of ${data.unattached.length} unattached disks selected`;
      bindUnattachedSelectionHandlers();
      updateSelectionCounts();
      refreshAllFilters();
    }

    function downloadBlob(content, filename, mimeType) {
      const blob = new Blob([content], { type: mimeType });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    }

    function escapeCsv(value) {
      const text = value == null ? "" : String(value);
      if (/[",\\n]/.test(text)) {
        return `"${text.replace(/"/g, '""')}"`;
      }
      return text;
    }

    function getSelectedUnattachedRows() {
      if (!state.data) {
        return [];
      }
      return state.data.unattached.filter(row => state.selectedUnattachedDiskIds.has(row.id));
    }

    function getSelectedMigrationRows() {
      if (!state.data) {
        return [];
      }
      const source = state.data.premiumLrsDataDisks || [];
      return source.filter(row => row.status === "green" && state.selectedMigrationDiskIds.has(row.id));
    }

    function exportInventoryCsv() {
      if (!state.data) {
        showNotice("Load a subscription first.", true);
        return;
      }
      const selectedRows = getSelectedUnattachedRows();
      const rows = selectedRows.length > 0 ? selectedRows : state.data.inventory;
      const headers = ["resourceGroup","diskName","location","sku","diskVersion","diskState","attached","vmName","isOsDisk","lun","caching","logicalSectorSize","burstingEnabled","unattached","id","portalUrl"];
      const lines = [headers.join(",")];
      for (const row of rows) {
        lines.push(headers.map(header => escapeCsv(row[header])).join(","));
      }
      const fileName = selectedRows.length > 0 ? "selected-unattached-disks.csv" : "disk-inventory.csv";
      downloadBlob(lines.join("\\n"), fileName, "text/csv;charset=utf-8");
    }

    function bindUnattachedSelectionHandlers() {
      for (const checkbox of document.querySelectorAll(".unattached-select")) {
        checkbox.addEventListener("change", (event) => {
          const diskId = event.target.dataset.diskId;
          if (event.target.checked) {
            state.selectedUnattachedDiskIds.add(diskId);
          } else {
            state.selectedUnattachedDiskIds.delete(diskId);
          }
          unattachedMetaEl.textContent = `${state.selectedUnattachedDiskIds.size} of ${state.data.unattached.length} unattached disks selected`;
          updateSelectionCounts();
        });
      }
    }

    function bindMigrationSelectionHandlers() {
      for (const checkbox of document.querySelectorAll(".migration-select")) {
        checkbox.addEventListener("change", (event) => {
          const diskId = event.target.dataset.diskId;
          if (event.target.checked) {
            state.selectedMigrationDiskIds.add(diskId);
          } else {
            state.selectedMigrationDiskIds.delete(diskId);
          }
          const source = state.data.premiumLrsDataDisks || [];
          const greenCount = source.filter(item => item.status === "green").length;
          const yellowCount = source.filter(item => item.status === "yellow").length;
          const redCount = source.filter(item => item.status === "red").length;
          migrationMetaEl.textContent = `${state.selectedMigrationDiskIds.size} of ${greenCount} green disks selected | Green: ${greenCount} | Yellow: ${yellowCount} | Red: ${redCount} | Total Premium LRS data disks: ${source.length}`;
          updateSelectionCounts();
        });
      }
    }

    async function deleteSelectedUnattached() {
      if (!state.data) {
        showNotice("Load a subscription first.", true);
        return;
      }

      const rows = getSelectedUnattachedRows();
      if (rows.length === 0) {
        showNotice("Select at least one unattached disk first.", true);
        return;
      }

      const impact = rows.map(r => `<li>${escapeHtml(r.diskName)}  <span style="color:var(--muted)">(${escapeHtml(r.resourceGroup)}, ${escapeHtml(r.location || "")}, ${escapeHtml(r.sku || "")})</span></li>`).join("");
      const confirmed = await showModal({
        title: `Delete ${rows.length} unattached disk(s)?`,
        danger: true,
        confirmWord: "DELETE",
        okLabel: "Permanently delete",
        bodyHtml: `
          <p>You are about to <strong>permanently delete</strong> the following unattached disk(s). This action <strong>cannot be undone</strong>.</p>
          <div class="modal-impact"><ul>${impact}</ul></div>
          <p>By proceeding you confirm that:</p>
          <ul>
            <li>You have the authority to delete these resources.</li>
            <li>You have verified the scope with the affected workload owners.</li>
            <li>You accept that this tool and its author bear <strong>no responsibility</strong> for data loss or service impact.</li>
          </ul>
        `,
      });
      if (!confirmed) {
        showNotice("Delete cancelled.");
        return;
      }

      try {
        showNotice("Deleting selected unattached disks...");
        renderOpLog([]);
        const { opId } = await postJson("/api/delete-unattached", {
          subscriptionId: state.data.subscriptionId,
          disks: rows.map(row => ({
            resourceGroup: row.resourceGroup,
            diskName: row.diskName,
            id: row.id
          }))
        });
        const outcome = await pollOperation(opId);
        if (outcome.ok) {
          state.selectedUnattachedDiskIds = new Set();
          await loadInventory();
          showNotice(`Deleted ${outcome.result.deletedCount} unattached disk(s).`);
        } else {
          showNotice(outcome.error, true);
        }
      } catch (error) {
        showNotice(error.message, true);
      }
    }

    async function backupSelectedMigration() {
      if (!state.data) {
        showNotice("Load a subscription first.", true);
        return;
      }
      const rows = getSelectedMigrationRows();
      if (rows.length === 0) {
        showNotice("Select at least one migration disk first.", true);
        return;
      }
      try {
        showNotice("Creating snapshots for selected disks...");
        renderOpLog([]);
        const { opId } = await postJson("/api/backup-disks", {
          subscriptionId: state.data.subscriptionId,
          disks: rows.map(row => ({ id: row.id, resourceGroup: row.resourceGroup, diskName: row.diskName }))
        });
        const outcome = await pollOperation(opId);
        if (outcome.ok) {
          showNotice(`Created ${outcome.result.snapshotCount} snapshot(s).`);
        } else {
          showNotice(outcome.error, true);
        }
      } catch (error) {
        showNotice(error.message, true);
      }
    }

    async function migrateSelectedDisks() {
      if (!state.data) {
        showNotice("Load a subscription first.", true);
        return;
      }
      const rows = getSelectedMigrationRows();
      if (rows.length === 0) {
        showNotice("Select at least one migration disk first.", true);
        return;
      }
      const createBackupBefore = document.getElementById("backupBeforeMigration").checked;
      const impact = rows.map(r => `<li>${escapeHtml(r.diskName)}  <span style="color:var(--muted)">(${escapeHtml(r.resourceGroup)}${r.vmName ? ", attached to " + escapeHtml(r.vmName) : ", unattached"})</span></li>`).join("");
      const backupLine = createBackupBefore
        ? `<li>Create a snapshot of each disk (<strong>Backup Before Migration is ON</strong>).</li>`
        : `<li><strong>No snapshot will be taken.</strong> Rollback will require an existing backup.</li>`;
      const confirmed = await showModal({
        title: `Migrate ${rows.length} disk(s) to PremiumV2_LRS?`,
        danger: true,
        confirmWord: "MIGRATE",
        okLabel: "Migrate now",
        bodyHtml: `
          <p>You are about to migrate the following disk(s) to <code>PremiumV2_LRS</code>:</p>
          <div class="modal-impact"><ul>${impact}</ul></div>
          <p>What will happen:</p>
          <ul>
            <li>Deallocate any VM that owns a selected disk (<strong>causes downtime</strong>).</li>
            ${backupLine}
            <li>Change each disk SKU to <code>PremiumV2_LRS</code> (in-place conversion).</li>
            <li>Restart the affected VM(s) once conversion completes.</li>
          </ul>
          <p>Azure may still reject the conversion at runtime for reasons not fully detectable in advance (capacity, RBAC, ASR replication, encryption settings, etc.).</p>
          <p>By proceeding you accept that this tool and its author bear <strong>no responsibility</strong> for data loss, downtime, or service impact, and you confirm the timing with the affected workload owners.</p>
        `,
      });
      if (!confirmed) {
        showNotice("Migration cancelled.");
        return;
      }
      try {
        showNotice("Migrating selected disks...");
        renderOpLog([]);
        const { opId } = await postJson("/api/migrate-disks", {
          subscriptionId: state.data.subscriptionId,
          createBackupBefore,
          disks: rows.map(row => ({ id: row.id, resourceGroup: row.resourceGroup, diskName: row.diskName }))
        });
        const outcome = await pollOperation(opId);
        if (outcome.ok) {
          state.selectedMigrationDiskIds = new Set();
          await loadInventory();
          showNotice(`Migrated ${outcome.result.migratedCount} disk(s) to PremiumV2_LRS.${outcome.result.snapshotCount ? ` Created ${outcome.result.snapshotCount} snapshot(s).` : ""}`);
        } else {
          showNotice(outcome.error, true);
        }
      } catch (error) {
        showNotice(error.message, true);
      }
    }

    async function loadInventory() {
      const subscriptionId = document.getElementById("subscription").value;
      const resourceGroupName = document.getElementById("resourceGroup").value.trim();

      if (!subscriptionId) {
        showNotice("Select a subscription first.", true);
        return;
      }

      const params = new URLSearchParams({ subscriptionId });
      if (resourceGroupName) {
        params.set("resourceGroupName", resourceGroupName);
      }

      try {
        clearNotice();
        showNotice("Loading Azure disks...");
        const data = await fetchJson(`/api/inventory?${params.toString()}`);
        renderData(data);
        clearNotice();
      } catch (error) {
        showNotice(error.message, true);
      }
    }

    document.getElementById("loadBtn").addEventListener("click", (ev) => withBusy(ev.currentTarget, loadInventory));
    document.getElementById("csvBtn").addEventListener("click", exportInventoryCsv);
    document.getElementById("backupSelectedBtn").addEventListener("click", (ev) => withBusy(ev.currentTarget, backupSelectedMigration));
    document.getElementById("migrateSelectedBtn").addEventListener("click", (ev) => withBusy(ev.currentTarget, migrateSelectedDisks));
    document.getElementById("deleteSelectedBtn").addEventListener("click", (ev) => withBusy(ev.currentTarget, deleteSelectedUnattached));
    navButtons.docs.addEventListener("click", () => setActiveView("docs"));
    navButtons.all.addEventListener("click", () => setActiveView("all"));
    navButtons.eligible.addEventListener("click", () => setActiveView("eligible"));
    navButtons.unattached.addEventListener("click", () => setActiveView("unattached"));

    for (const id of ["searchInventory", "searchEligible", "searchUnattached"]) {
      document.getElementById(id).addEventListener("input", refreshAllFilters);
    }

    setActiveView("docs");
    updateSelectionCounts();

    async function loadConfig() {
      try {
        const cfg = await fetchJson("/api/config");
        state.config = cfg;
        const banner = document.getElementById("modeBanner");
        if (cfg.readOnly) {
          banner.textContent = "READ-ONLY MODE — destructive actions are disabled on the server.";
          banner.className = "mode-banner read-only";
          banner.hidden = false;
          for (const id of ["backupSelectedBtn","migrateSelectedBtn","deleteSelectedBtn"]) {
            document.getElementById(id).disabled = true;
          }
        } else if (cfg.dryRun) {
          banner.textContent = "DRY-RUN MODE — Azure state changes will be logged but not executed.";
          banner.className = "mode-banner dry-run";
          banner.hidden = false;
        }
        if (cfg.authRequired && !localStorage.getItem("dashboard-token")) {
          const accepted = await showModal({
            title: "Authentication required",
            okLabel: "Save token",
            cancelLabel: "Continue anonymously",
            bodyHtml: `
              <p>This server is configured to require an authentication token for state-changing actions. Paste the token printed on the server console:</p>
              <p><input id="tokenEntry" style="width:100%;min-height:36px;padding:8px 12px;border-radius:8px;border:1px solid var(--line);font-family:ui-monospace,monospace;" autocomplete="off"></p>
              <p style="color: var(--muted); font-size: 0.82rem;">Stored in your browser only. Read-only endpoints work without a token.</p>
            `,
          });
          if (accepted) {
            const tok = (document.getElementById("tokenEntry") || {}).value;
            if (tok) localStorage.setItem("dashboard-token", tok.trim());
          }
        }
      } catch (e) {
        // Server config endpoint not reachable; keep dashboard usable but warn.
      }
    }

    async function ensureDisclaimerAccepted() {
      if (localStorage.getItem("disclaimer-accepted")) return;
      document.body.classList.add("disclaimer-pending");
      const accepted = await showModal({
        title: "Disclaimer & limitation of liability",
        dismissible: false,
        confirmWord: "I AGREE",
        okLabel: "Accept and continue",
        cancelLabel: "Exit",
        bodyHtml: `
          <p>This software is provided <strong>"AS IS"</strong>, without warranty of any kind. The author and contributors accept no liability for any data loss, service interruption, downtime, or financial impact arising from the use of, or inability to use, this software.</p>
          <p>By using the <strong>Migrate</strong>, <strong>Backup</strong>, or <strong>Delete</strong> features you acknowledge that:</p>
          <ul>
            <li>You have appropriate authorization to perform these actions on the target Azure resources.</li>
            <li>You have verified the selected scope and confirmed the impact with the affected workload owners.</li>
            <li>You have taken independent backups before destructive operations.</li>
            <li>The displayed eligibility status is a best-effort evaluation and may not capture every runtime constraint Azure enforces.</li>
            <li>Migration and deletion actions are <strong>irreversible without prior backups</strong>.</li>
          </ul>
          <p>Your acceptance is stored in this browser; you will not be prompted again unless you clear site data.</p>
        `,
      });
      if (accepted) {
        localStorage.setItem("disclaimer-accepted", new Date().toISOString());
        document.body.classList.remove("disclaimer-pending");
      } else {
        document.body.innerHTML = "<div style='padding:60px;text-align:center;font-family:system-ui;color:#475569;'><h2>Session ended</h2><p>You declined the disclaimer. Close this tab to exit.</p></div>";
      }
    }

    (async () => {
      await loadConfig();
      await ensureDisclaimerAccepted();
      loadSubscriptions();
    })();
  </script>
</body>
</html>
"""


def run_az_json(arguments, subscription_id=None, timeout_seconds=90):
    if DRY_RUN_MODE and is_state_changing(arguments):
        # In dry-run mode, log the would-be command and return an empty dict.
        return {"dryRun": True, "command": list(arguments)}

    az_executable = shutil.which("az") or shutil.which("az.cmd")
    if not az_executable:
        raise RuntimeError("Azure CLI executable was not found. Install Azure CLI or restart the terminal after installation.")

    env = os.environ.copy()

    command = [az_executable, *arguments, "--only-show-errors", "-o", "json"]
    if subscription_id:
        command.extend(["--subscription", subscription_id])

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        env=env,
        timeout=timeout_seconds,
    )

    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or "Azure CLI command failed"
        raise RuntimeError(error)

    raw_output = completed.stdout.strip()
    if not raw_output:
        return None
    return json.loads(raw_output)


def get_cached_entry(bucket, key):
    with _cache_lock:
        entry = _cache[bucket].get(key)
        if not entry:
            return None
        if entry["expires_at"] < time.monotonic():
            del _cache[bucket][key]
            return None
        return entry["value"]


def set_cached_entry(bucket, key, value, ttl_seconds):
    with _cache_lock:
        _cache[bucket][key] = {
            "value": value,
            "expires_at": time.monotonic() + ttl_seconds,
        }


def get_subscriptions():
    with _cache_lock:
        entry = _cache["subscriptions"]
        if entry and entry["expires_at"] >= time.monotonic():
            return entry["value"]

    accounts = run_az_json(["account", "list"])
    subscriptions = []
    for account in accounts or []:
        subscriptions.append(
            {
                "id": account.get("id"),
                "name": account.get("name"),
                "isDefault": account.get("isDefault", False),
                "tenantId": account.get("tenantId"),
                "state": account.get("state"),
            }
        )

    subscriptions.sort(key=lambda item: (not item["isDefault"], item["name"] or ""))
    with _cache_lock:
        _cache["subscriptions"] = {
            "value": subscriptions,
            "expires_at": time.monotonic() + SUBSCRIPTION_CACHE_TTL_SECONDS,
        }
    return subscriptions


def get_disk_version(sku_name):
    if sku_name == "Premium_LRS":
        return "V1"
    if sku_name == "PremiumV2_LRS":
        return "V2"
    return "Other"


def get_double_encryption_enabled(disk):
    encryption = disk.get("encryption") or {}
    encryption_type = (encryption.get("type") or "").lower()
    return "platformandcustomerkeys" in encryption_type or "doubleencryption" in encryption_type


def get_asr_enabled(disk):
    tags = disk.get("tags") or {}
    for key, value in tags.items():
        haystack = f"{key} {value}".lower()
        if "asr" in haystack or "siterecovery" in haystack or "recoveryservice" in haystack:
            return True
    name = (disk.get("name") or "").lower()
    if "-asr-" in name or name.endswith("-asr") or "asrseeddisk" in name:
        return True
    return False


def compute_v2_status(region_supported, sector_not_512, caching_on, bursting_on, double_enc_on, asr_on):
    if region_supported is False:
        return "red"
    issues = [sector_not_512, caching_on, bursting_on, double_enc_on, asr_on]
    if region_supported is True and not any(issues):
        return "green"
    return "yellow"


def build_portal_disk_url(disk_id):
    if not disk_id:
        return None
    return f"https://portal.azure.com/#@/resource{disk_id}/overview"


def get_resource_name_from_id(resource_id):
    if not resource_id:
        return None
    parts = [part for part in str(resource_id).split("/") if part]
    if not parts:
        return None
    return parts[-1]


def invalidate_payload_cache(subscription_id):
    with _cache_lock:
        keys_to_delete = [key for key in _cache["payloads"] if key[0] == subscription_id]
        for key in keys_to_delete:
            del _cache["payloads"][key]


def get_region_support(subscription_id):
    cached = get_cached_entry("region_support", subscription_id)
    if cached is not None:
        return cached

    try:
        skus = run_az_json(["vm", "list-skus", "--resource-type", "disks"], subscription_id, timeout_seconds=300)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None

    regions = set()
    for sku in skus or []:
        if sku.get("name") != "PremiumV2_LRS":
            continue
        for location_info in sku.get("locationInfo") or []:
            location = location_info.get("location")
            if location:
                regions.add(location)
    set_cached_entry("region_support", subscription_id, regions, REGION_SUPPORT_CACHE_TTL_SECONDS)
    return regions


def build_vm_disk_map(virtual_machines):
    mapping = {}
    for vm in virtual_machines or []:
        storage_profile = vm.get("storageProfile") or {}
        os_disk = storage_profile.get("osDisk") or {}
        os_managed_disk = os_disk.get("managedDisk") or {}
        os_disk_id = os_managed_disk.get("id")
        if os_disk_id:
            mapping[os_disk_id.lower()] = {
                "vmName": vm.get("name"),
                "resourceGroup": vm.get("resourceGroup"),
                "isOsDisk": True,
                "lun": None,
                "caching": os_disk.get("caching"),
            }

        for data_disk in storage_profile.get("dataDisks") or []:
            managed_disk = data_disk.get("managedDisk") or {}
            disk_id = managed_disk.get("id")
            if not disk_id:
                continue
            mapping[disk_id.lower()] = {
                "vmName": vm.get("name"),
                "resourceGroup": vm.get("resourceGroup"),
                "isOsDisk": False,
                "lun": data_disk.get("lun"),
                "caching": data_disk.get("caching"),
            }
    return mapping


def get_inventory(subscription_id, resource_group_name=None, include_region_support=False):
    vm_args = ["vm", "list"]
    if resource_group_name:
        vm_args.extend(["--resource-group", resource_group_name])

    virtual_machines = run_az_json(vm_args, subscription_id) or []
    vm_disk_map = build_vm_disk_map(virtual_machines)

    if resource_group_name:
        resource_groups = [resource_group_name]
    else:
        groups = run_az_json(["group", "list"], subscription_id) or []
        resource_groups = [group.get("name") for group in groups if group.get("name")]

    disks = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                run_az_json,
                ["disk", "list", "--resource-group", group_name],
                subscription_id,
            )
            for group_name in resource_groups
        ]
        for future in futures:
            result = future.result() or []
            disks.extend(result)

    requires_region_check = include_region_support and any(((disk.get("sku") or {}).get("name")) == "Premium_LRS" for disk in disks)
    supported_regions = get_region_support(subscription_id) if requires_region_check else None

    inventory = []
    for disk in disks:
        disk_id = (disk.get("id") or "").lower()
        attachment = vm_disk_map.get(disk_id)
        managed_by = disk.get("managedBy")
        disk_state = disk.get("diskState")
        attached = bool(managed_by) or attachment is not None or disk_state == "Attached"
        sku_name = ((disk.get("sku") or {}).get("name")) or ""
        location = disk.get("location")
        derived_vm_name = attachment.get("vmName") if attachment else get_resource_name_from_id(managed_by)

        inventory.append(
            {
                "subscriptionId": subscription_id,
                "resourceGroup": disk.get("resourceGroup"),
                "diskName": disk.get("name"),
                "location": location,
                "sku": sku_name,
                "diskVersion": get_disk_version(sku_name),
                "diskState": disk_state,
                "managedBy": managed_by,
                "attached": attached,
                "vmName": derived_vm_name,
                "isOsDisk": bool((attachment and attachment.get("isOsDisk")) or disk.get("osType")),
                "osType": disk.get("osType"),
                "lun": attachment.get("lun") if attachment else None,
                "caching": attachment.get("caching") if attachment else None,
                "logicalSectorSize": disk.get("logicalSectorSize") if disk.get("logicalSectorSize") is not None else 512,
                "burstingEnabled": disk.get("burstingEnabled"),
                "doubleEncryptionEnabled": get_double_encryption_enabled(disk),
                "asrEnabled": get_asr_enabled(disk),
                "unattached": not attached,
                "premiumV2RegionSupported": None if supported_regions is None else location in supported_regions,
                "id": disk.get("id"),
                "portalUrl": build_portal_disk_url(disk.get("id")),
            }
        )

    return inventory, requires_region_check


def build_premium_lrs_data_view(inventory):
    rows = []
    for disk in inventory:
        if disk["sku"] != "Premium_LRS":
            continue
        if disk["isOsDisk"]:
            continue

        region_supported = disk["premiumV2RegionSupported"]
        sector_not_512 = disk["logicalSectorSize"] != 512
        caching_on = bool(disk["caching"]) and disk["caching"] != "None"
        bursting_on = disk["burstingEnabled"] is True
        double_enc_on = bool(disk["doubleEncryptionEnabled"])
        asr_on = bool(disk["asrEnabled"])

        status = compute_v2_status(region_supported, sector_not_512, caching_on, bursting_on, double_enc_on, asr_on)

        workarounds = []
        if sector_not_512:
            workarounds.append(f"Sector size '{disk['logicalSectorSize']}' is not 512 — workaround: create new V2 disk")
        if caching_on:
            workarounds.append(f"Host caching '{disk['caching']}' must be disabled")
        if bursting_on:
            workarounds.append("Bursting must be disabled")
        if double_enc_on:
            workarounds.append("Double encryption must be disabled")
        if asr_on:
            workarounds.append("ASR must be disabled before changing to V2")
        if region_supported is False:
            workarounds.insert(0, f"Region '{disk['location']}' does not support Premium SSD v2")
        elif region_supported is None:
            workarounds.append("Region support could not be verified from Azure CLI")

        rows.append(
            {
                "id": disk["id"],
                "resourceGroup": disk["resourceGroup"],
                "vmName": disk["vmName"],
                "diskName": disk["diskName"],
                "location": disk["location"],
                "regionSupported": region_supported,
                "sectorNot512": sector_not_512,
                "logicalSectorSize": disk["logicalSectorSize"],
                "hostCachingEnabled": caching_on,
                "caching": disk["caching"],
                "burstingEnabled": bursting_on,
                "doubleEncryptionEnabled": double_enc_on,
                "asrEnabled": asr_on,
                "status": status,
                "notes": "; ".join(workarounds) if workarounds else "Ready for direct conversion",
                "portalUrl": disk["portalUrl"],
            }
        )
    return rows


def get_migration_plan(inventory):
    plan = []
    for disk in inventory:
        reasons = []
        eligible = True

        if disk["diskVersion"] == "V2":
            eligible = False
            reasons.append("Disk is already Premium SSD v2")
        elif disk["diskVersion"] != "V1":
            eligible = False
            reasons.append(f"SKU '{disk['sku']}' is not Premium_LRS")
        else:
            if disk["premiumV2RegionSupported"] is False:
                eligible = False
                reasons.append(f"Region '{disk['location']}' does not report Premium SSD v2 availability")
            if disk["isOsDisk"]:
                eligible = False
                reasons.append("OS disks cannot be converted to Premium SSD v2")
            if disk["logicalSectorSize"] != 512:
                eligible = False
                reasons.append(f"Logical sector size '{disk['logicalSectorSize']}' is not supported for direct conversion")
            if disk["burstingEnabled"] is True:
                eligible = False
                reasons.append("Bursting is enabled")
            if disk["caching"] and disk["caching"] != "None":
                eligible = False
                reasons.append(f"Host caching '{disk['caching']}' must be disabled first")
            if disk["premiumV2RegionSupported"] is None:
                reasons.append("Region support could not be verified from Azure CLI")

        plan.append(
            {
                "resourceGroup": disk["resourceGroup"],
                "vmName": disk["vmName"],
                "diskName": disk["diskName"],
                "eligible": eligible,
                "plannedSku": "PremiumV2_LRS" if eligible else None,
                "reasons": "; ".join(reasons) if reasons else "Ready for conversion",
                "id": disk["id"],
                "portalUrl": disk["portalUrl"],
            }
        )
    return plan


def build_payload(subscription_id, resource_group_name=None):
    cache_key = (subscription_id, resource_group_name or "")
    cached_payload = get_cached_entry("payloads", cache_key)
    if cached_payload is not None:
        payload = dict(cached_payload)
        payload["cacheHit"] = True
        return payload

    started_at = time.perf_counter()
    inventory, region_support_checked = get_inventory(subscription_id, resource_group_name, include_region_support=True)
    migration_plan = get_migration_plan(inventory)
    premium_lrs_data_disks = build_premium_lrs_data_view(inventory)
    unattached = [item for item in inventory if item["unattached"]]
    summary = {
        "totalDisks": len(inventory),
        "v1Disks": sum(1 for item in inventory if item["diskVersion"] == "V1"),
        "v2Disks": sum(1 for item in inventory if item["diskVersion"] == "V2"),
        "otherDisks": sum(1 for item in inventory if item["diskVersion"] == "Other"),
        "eligibleDisks": sum(1 for item in premium_lrs_data_disks if item["status"] == "green"),
        "premiumLrsDataDisks": len(premium_lrs_data_disks),
        "premiumLrsGreen": sum(1 for item in premium_lrs_data_disks if item["status"] == "green"),
        "premiumLrsYellow": sum(1 for item in premium_lrs_data_disks if item["status"] == "yellow"),
        "premiumLrsRed": sum(1 for item in premium_lrs_data_disks if item["status"] == "red"),
        "unattachedDisks": len(unattached),
    }

    payload = {
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "subscriptionId": subscription_id,
        "resourceGroupName": resource_group_name,
        "summary": summary,
        "inventory": inventory,
        "migrationPlan": migration_plan,
        "premiumLrsDataDisks": premium_lrs_data_disks,
        "unattached": unattached,
        "durationMs": int((time.perf_counter() - started_at) * 1000),
        "cacheHit": False,
        "regionSupportChecked": region_support_checked,
    }
    set_cached_entry("payloads", cache_key, payload, PAYLOAD_CACHE_TTL_SECONDS)
    return payload


def _log(log, message):
    if log is None:
        return
    log.append({
        "ts": datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S"),
        "message": message,
    })


def _start_operation(target, args):
    op_id = secrets.token_hex(8)
    op = {
        "id": op_id,
        "status": "running",
        "log": [],
        "result": None,
        "error": None,
        "createdAt": time.time(),
    }
    with _operations_lock:
        _prune_old_operations_locked()
        _operations[op_id] = op

    def runner():
        try:
            result = target(*args, op["log"])
            op["result"] = result
            op["status"] = "complete"
        except Exception as exc:
            _log(op["log"], f"ERROR: {exc}")
            op["error"] = str(exc)
            op["status"] = "failed"

    threading.Thread(target=runner, daemon=True).start()
    return op_id


def _prune_old_operations_locked():
    cutoff = time.time() - OPERATION_RETENTION_SECONDS
    for key in list(_operations.keys()):
        op = _operations[key]
        if op["status"] != "running" and op["createdAt"] < cutoff:
            del _operations[key]


def _get_operation_status(op_id):
    with _operations_lock:
        op = _operations.get(op_id)
        if not op:
            return None
        return {
            "id": op["id"],
            "status": op["status"],
            "log": list(op["log"]),
            "result": op["result"],
            "error": op["error"],
        }


def delete_unattached_disks(subscription_id, disks, log=None):
    deleted_count = 0
    if len(disks) > MAX_MIGRATION_BATCH:
        raise RuntimeError(f"Batch size {len(disks)} exceeds limit of {MAX_MIGRATION_BATCH}.")
    if DRY_RUN_MODE:
        _log(log, "DRY-RUN: state changes will be logged but not executed")
    _log(log, f"Deleting {len(disks)} unattached disk(s)")

    for disk in disks:
        resource_group = disk.get("resourceGroup")
        disk_name = disk.get("diskName")
        if not resource_group or not disk_name:
            raise RuntimeError("Each disk must include 'resourceGroup' and 'diskName'.")

        _log(log, f"Verifying '{disk_name}' is unattached")
        current_disk = run_az_json(
            ["disk", "show", "--resource-group", resource_group, "--name", disk_name],
            subscription_id,
        )

        if current_disk.get("managedBy") or current_disk.get("diskState") == "Attached":
            raise RuntimeError(f"Disk '{disk_name}' is attached and cannot be deleted from this action.")

        _log(log, f"Deleting '{disk_name}'")
        run_az_json(
            ["disk", "delete", "--resource-group", resource_group, "--name", disk_name, "--yes"],
            subscription_id,
            timeout_seconds=600,
        )
        deleted_count += 1
        _log(log, f"Deleted '{disk_name}'")

    invalidate_payload_cache(subscription_id)
    _log(log, f"Done. Deleted {deleted_count} disk(s).")
    audit_path = write_audit_entry(
        "delete",
        subscription_id,
        disks,
        result={"deletedCount": deleted_count},
        dry_run=DRY_RUN_MODE,
    )
    if audit_path:
        _log(log, f"Audit entry written to {audit_path}")
    return {"deletedCount": deleted_count}


def create_disk_snapshot(subscription_id, resource_group, disk_name, source_disk_id, location, sku_name):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    max_disk_name_length = 60
    snapshot_disk_segment = disk_name[:max_disk_name_length]
    snapshot_name = f"snapshot-{snapshot_disk_segment}-{timestamp}"
    run_az_json(
        [
            "snapshot",
            "create",
            "--resource-group",
            resource_group,
            "--name",
            snapshot_name,
            "--location",
            location,
            "--source",
            source_disk_id,
            "--sku",
            sku_name,
        ],
        subscription_id,
        timeout_seconds=600,
    )
    return snapshot_name


def migrate_disks(subscription_id, disks, create_backup_before=False, log=None):
    migrated_count = 0
    snapshot_count = 0
    if len(disks) > MAX_MIGRATION_BATCH:
        raise RuntimeError(f"Batch size {len(disks)} exceeds limit of {MAX_MIGRATION_BATCH}.")
    if DRY_RUN_MODE:
        _log(log, "DRY-RUN: state changes will be logged but not executed")
    _log(log, f"Starting migration of {len(disks)} disk(s){' with backup' if create_backup_before else ''}")
    _log(log, "Listing VMs to map attachments")
    vm_cache = run_az_json(["vm", "list"], subscription_id) or []
    vm_disk_map = build_vm_disk_map(vm_cache)
    deallocated_vm_keys = set()
    deallocated_vms = []

    def ensure_vm_deallocated(resource_group, vm_name):
        vm_key = (resource_group.lower(), vm_name.lower())
        if vm_key in deallocated_vm_keys:
            return
        _log(log, f"Deallocating VM '{vm_name}'")
        run_az_json(
            ["vm", "deallocate", "--resource-group", resource_group, "--name", vm_name],
            subscription_id,
            timeout_seconds=900,
        )
        deallocated_vm_keys.add(vm_key)
        deallocated_vms.append({"resourceGroup": resource_group, "vmName": vm_name})
        _log(log, f"VM '{vm_name}' deallocated")

    try:
        for disk in disks:
            resource_group = disk.get("resourceGroup")
            disk_name = disk.get("diskName")
            if not resource_group or not disk_name:
                raise RuntimeError("Each disk must include 'resourceGroup' and 'diskName'.")

            _log(log, f"Inspecting disk '{disk_name}'")
            current_disk = run_az_json(
                ["disk", "show", "--resource-group", resource_group, "--name", disk_name],
                subscription_id,
            )

            sku_name = ((current_disk.get("sku") or {}).get("name")) or ""
            if sku_name != "Premium_LRS":
                raise RuntimeError(f"Disk '{disk_name}' is not Premium_LRS.")
            if current_disk.get("osType"):
                raise RuntimeError(f"Disk '{disk_name}' appears to be an OS disk and cannot be migrated to Premium SSD v2.")
            sector_size = current_disk.get("logicalSectorSize")
            if sector_size is not None and sector_size != 512:
                raise RuntimeError(f"Disk '{disk_name}' does not have a supported logical sector size for direct conversion.")
            if current_disk.get("burstingEnabled") is True:
                raise RuntimeError(f"Disk '{disk_name}' has bursting enabled.")

            attachment = vm_disk_map.get((current_disk.get("id") or "").lower())
            if attachment and attachment.get("isOsDisk"):
                raise RuntimeError(f"Disk '{disk_name}' is attached as an OS disk and cannot be migrated.")
            if attachment and attachment.get("caching") and attachment.get("caching") != "None":
                raise RuntimeError(f"Disk '{disk_name}' must have host caching set to None before migration.")

            if create_backup_before:
                _log(log, f"Creating snapshot for '{disk_name}'")
                snap_name = create_disk_snapshot(
                    subscription_id,
                    resource_group,
                    disk_name,
                    current_disk["id"],
                    current_disk["location"],
                    sku_name,
                )
                snapshot_count += 1
                _log(log, f"Snapshot '{snap_name}' created")

            if attachment and attachment.get("vmName"):
                ensure_vm_deallocated(attachment["resourceGroup"], attachment["vmName"])

            _log(log, f"Updating SKU of '{disk_name}' to PremiumV2_LRS")
            run_az_json(
                ["disk", "update", "--resource-group", resource_group, "--name", disk_name, "--sku", "PremiumV2_LRS"],
                subscription_id,
                timeout_seconds=600,
            )
            migrated_count += 1
            _log(log, f"Migrated '{disk_name}' to PremiumV2_LRS")
    finally:
        for vm_target in deallocated_vms:
            _log(log, f"Starting VM '{vm_target['vmName']}'")
            run_az_json(
                ["vm", "start", "--resource-group", vm_target["resourceGroup"], "--name", vm_target["vmName"]],
                subscription_id,
                timeout_seconds=900,
            )
            _log(log, f"VM '{vm_target['vmName']}' started")

    invalidate_payload_cache(subscription_id)
    _log(log, f"Done. Migrated {migrated_count} disk(s); created {snapshot_count} snapshot(s).")
    audit_path = write_audit_entry(
        "migrate",
        subscription_id,
        disks,
        result={"migratedCount": migrated_count, "snapshotCount": snapshot_count},
        dry_run=DRY_RUN_MODE,
    )
    if audit_path:
        _log(log, f"Audit entry written to {audit_path}")
    return {"migratedCount": migrated_count, "snapshotCount": snapshot_count}


def backup_disks(subscription_id, disks, log=None):
    snapshot_count = 0
    if len(disks) > MAX_MIGRATION_BATCH:
        raise RuntimeError(f"Batch size {len(disks)} exceeds limit of {MAX_MIGRATION_BATCH}.")
    if DRY_RUN_MODE:
        _log(log, "DRY-RUN: state changes will be logged but not executed")
    _log(log, f"Creating snapshots for {len(disks)} disk(s)")
    for disk in disks:
        resource_group = disk.get("resourceGroup")
        disk_name = disk.get("diskName")
        if not resource_group or not disk_name:
            raise RuntimeError("Each disk must include 'resourceGroup' and 'diskName'.")

        _log(log, f"Inspecting '{disk_name}'")
        current_disk = run_az_json(
            ["disk", "show", "--resource-group", resource_group, "--name", disk_name],
            subscription_id,
        )
        _log(log, f"Creating snapshot for '{disk_name}'")
        snap_name = create_disk_snapshot(
            subscription_id,
            resource_group,
            disk_name,
            current_disk["id"],
            current_disk["location"],
            ((current_disk.get("sku") or {}).get("name")) or "Standard_LRS",
        )
        snapshot_count += 1
        _log(log, f"Snapshot '{snap_name}' created")

    _log(log, f"Done. Created {snapshot_count} snapshot(s).")
    audit_path = write_audit_entry(
        "backup",
        subscription_id,
        disks,
        result={"snapshotCount": snapshot_count},
        dry_run=DRY_RUN_MODE,
    )
    if audit_path:
        _log(log, f"Audit entry written to {audit_path}")
    return {"snapshotCount": snapshot_count}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(HTML.encode("utf-8"))
                return

            if parsed.path == "/api/config":
                return self.write_json({
                    "readOnly": READ_ONLY_MODE,
                    "dryRun": DRY_RUN_MODE,
                    "authRequired": bool(AUTH_TOKEN),
                    "maxMigrationBatch": MAX_MIGRATION_BATCH,
                    "auditDir": AUDIT_DIR,
                })

            if parsed.path == "/api/subscriptions":
                return self.write_json({"subscriptions": get_subscriptions()})

            if parsed.path == "/api/inventory":
                query = parse_qs(parsed.query)
                subscription_id = (query.get("subscriptionId") or [None])[0]
                resource_group_name = (query.get("resourceGroupName") or [None])[0]
                if not subscription_id:
                    return self.write_json({"error": "Missing required query parameter 'subscriptionId'."}, HTTPStatus.BAD_REQUEST)
                return self.write_json(build_payload(subscription_id, resource_group_name))

            if parsed.path.startswith("/api/op-status/"):
                op_id = parsed.path.rsplit("/", 1)[-1]
                status = _get_operation_status(op_id)
                if status is None:
                    return self.write_json({"error": "Operation not found"}, HTTPStatus.NOT_FOUND)
                return self.write_json(status)

            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
        except Exception as error:
            if not self._is_client_disconnect(error):
                self.write_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            payload = json.loads(raw_body.decode("utf-8"))

            # Auth guard: state-changing endpoints require the configured token if any.
            if AUTH_TOKEN and parsed.path.startswith("/api/"):
                provided = self.headers.get("X-Dashboard-Token") or ""
                if provided != AUTH_TOKEN:
                    return self.write_json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)

            # Read-only guard: refuse state-changing operations.
            if READ_ONLY_MODE and parsed.path in ("/api/delete-unattached", "/api/backup-disks", "/api/migrate-disks"):
                return self.write_json(
                    {"error": "Dashboard is in read-only mode. Set DISK_DASHBOARD_READONLY=0 to allow writes."},
                    HTTPStatus.FORBIDDEN,
                )

            if parsed.path == "/api/delete-unattached":
                subscription_id = payload.get("subscriptionId")
                disks = payload.get("disks") or []
                if not subscription_id:
                    return self.write_json({"error": "Missing 'subscriptionId'."}, HTTPStatus.BAD_REQUEST)
                if not disks:
                    return self.write_json({"error": "No disks were provided for deletion."}, HTTPStatus.BAD_REQUEST)
                op_id = _start_operation(delete_unattached_disks, [subscription_id, disks])
                return self.write_json({"opId": op_id}, HTTPStatus.ACCEPTED)

            if parsed.path == "/api/backup-disks":
                subscription_id = payload.get("subscriptionId")
                disks = payload.get("disks") or []
                if not subscription_id:
                    return self.write_json({"error": "Missing 'subscriptionId'."}, HTTPStatus.BAD_REQUEST)
                if not disks:
                    return self.write_json({"error": "No disks were provided for backup."}, HTTPStatus.BAD_REQUEST)
                op_id = _start_operation(backup_disks, [subscription_id, disks])
                return self.write_json({"opId": op_id}, HTTPStatus.ACCEPTED)

            if parsed.path == "/api/migrate-disks":
                subscription_id = payload.get("subscriptionId")
                disks = payload.get("disks") or []
                create_backup_before = bool(payload.get("createBackupBefore"))
                if not subscription_id:
                    return self.write_json({"error": "Missing 'subscriptionId'."}, HTTPStatus.BAD_REQUEST)
                if not disks:
                    return self.write_json({"error": "No disks were provided for migration."}, HTTPStatus.BAD_REQUEST)
                op_id = _start_operation(migrate_disks, [subscription_id, disks, create_backup_before])
                return self.write_json({"opId": op_id}, HTTPStatus.ACCEPTED)

            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
        except Exception as error:
            if not self._is_client_disconnect(error):
                self.write_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format, *args):
        return

    def write_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:
            if not self._is_client_disconnect(error):
                raise

    @staticmethod
    def _is_client_disconnect(error):
        return isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.error))


def main():
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Azure Disk Dashboard running at http://{HOST}:{PORT}")
    print(f"  Mode:         {'READ-ONLY' if READ_ONLY_MODE else 'READ-WRITE'}"
          f"{' + DRY-RUN' if DRY_RUN_MODE else ''}")
    print(f"  Auth:         {'token required (X-Dashboard-Token header)' if AUTH_TOKEN else 'open'}")
    print(f"  Batch limit:  {MAX_MIGRATION_BATCH} disks per migrate/backup/delete")
    print(f"  Audit log:    {AUDIT_DIR}")
    print("Use Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
