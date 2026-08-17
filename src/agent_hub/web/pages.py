from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote
from html import escape
import json
import time
from typing import Any


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value))


def _display_title(payload: dict[str, Any]) -> str:
    """Title cell: the real session title when present, otherwise a muted
    fallback (workspace directory name, then 'untitled'). Untitled sessions
    are common — test sessions and worker task sessions never emit a title
    event — so the directory shows something recognizable instead of '-'."""
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return _esc(title)
    workspace = payload.get("workspace")
    if isinstance(workspace, str):
        base = workspace.strip().rstrip("/").rsplit("/", 1)[-1]
        if base:
            return f'<span class="muted">({_esc(base)})</span>'
    return '<span class="muted">(untitled)</span>'


def _norm_ts(timestamp: float | None) -> float | None:
    """Normalize epoch timestamps: milliseconds (directory rows use JS
    Date.now()) to seconds. Second-epoch values are far below 1e11, so the
    threshold never misfires for regular Hub timestamps."""
    if timestamp is None:
        return None
    value = float(timestamp)
    if value > 1e11:
        value /= 1000
    return value


def _fmt(timestamp: float | None) -> str:
    value = _norm_ts(timestamp)
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _fmt_relative(timestamp: float | None) -> str:
    """Short human-friendly time: 'just now', '5 min ago', or a date."""

    value = _norm_ts(timestamp)
    if value is None:
        return "-"
    delta = time.time() - value
    if delta < 0:
        return _fmt(value)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)} d ago"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d")


def _short_id(value: Any, width: int = 12) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else f"{text[:width]}…"


def _status_pill(status: Any) -> str:
    return (
        f'<span class="pill status-{_esc(status)}" '
        f'title="status">{_esc(status)}</span>'
    )


def _json_text(value: Any) -> str:
    return _esc(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


_CSS = """
:root { color-scheme: light dark; --accent: #2563eb; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0;
       background: #f8fafc; color: #0f172a; }
.topbar { background: #0f172a; color: #f8fafc; padding: 0.75rem 1.25rem;
          display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
.topbar a { color: #cbd5e1; text-decoration: none; margin-right: 0.75rem; }
.topbar a:hover { color: #fff; }
.topbar a.active { color: #fff; border-bottom: 2px solid var(--accent); }
.topbar form { margin: 0; }
.content { max-width: 1100px; margin: 1.5rem auto; padding: 0 1.25rem; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { border: 1px solid #e2e8f0; padding: 0.5rem 0.65rem; text-align: left;
         font-size: 0.9rem; vertical-align: top; }
th { background: #f1f5f9; }
tr:nth-child(even) td { background: #f8fafc; }
h1 { font-size: 1.35rem; }
h2 { font-size: 1.1rem; margin-top: 1.5rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 0.75rem; margin: 1rem 0; }
.card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
        padding: 0.9rem; }
.card b { display: block; font-size: 1.5rem; color: var(--accent); }
.card span { font-size: 0.8rem; color: #64748b; }
.pill { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
        font-size: 0.75rem; background: #e0e7ff; color: #3730a3; }
.pill.status-online { background: #dcfce7; color: #166534; }
.pill.status-offline { background: #e2e8f0; color: #475569; }
.pill.status-submitted { background: #fef3c7; color: #92400e; }
.pill.status-working { background: #dbeafe; color: #1d4ed8; }
.pill.status-completed { background: #dcfce7; color: #166534; }
.pill.status-failed { background: #fee2e2; color: #991b1b; }
.pill.status-cancelled { background: #e2e8f0; color: #475569; }
.pill.status-active { background: #dbeafe; color: #1d4ed8; }
.pill.status-pending { background: #fef3c7; color: #92400e; }
.pill.status-claimed { background: #dbeafe; color: #1d4ed8; }
.pill.status-answered { background: #dcfce7; color: #166534; }
.pill.status-expired { background: #e2e8f0; color: #475569; }
.pill.status-unsupported { background: #f3e8ff; color: #6b21a8; }
.pill.status-declined { background: #fee2e2; color: #991b1b; }
.short-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.82rem; }
.time { white-space: nowrap; color: #64748b; font-size: 0.82rem; }
details { margin: 0.75rem 0; }
details summary { cursor: pointer; color: var(--accent); }
pre { background: #0f172a; color: #e2e8f0; padding: 0.75rem; overflow-x: auto;
      border-radius: 6px; font-size: 0.8rem; }
input, textarea, select { width: 100%; padding: 0.45rem 0.6rem; margin: 0.25rem 0 0.75rem;
                          border: 1px solid #cbd5e1; border-radius: 6px;
                          font: inherit; }
button { background: var(--accent); color: #fff; border: 0; padding: 0.5rem 1rem;
         border-radius: 6px; cursor: pointer; font: inherit; }
button.secondary { background: #64748b; }
.error { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca;
         padding: 0.75rem; border-radius: 6px; }
.notice { background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0;
          padding: 0.75rem; border-radius: 6px; }
.muted { color: #64748b; font-size: 0.85rem; }
.hero { background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%);
        color: #f8fafc; padding: 2.5rem 1.5rem; border-radius: 10px;
        margin: 1.5rem 0; }
.hero h1 { margin: 0 0 0.5rem; font-size: 1.8rem; }
.hero p { margin: 0.25rem 0; color: #cbd5e1; max-width: 760px; }
.hero .tag { display: inline-block; margin-top: 0.75rem; background: #2563eb;
             padding: 0.2rem 0.7rem; border-radius: 999px; font-size: 0.75rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 0.9rem; margin: 1rem 0; }
.panel { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
         padding: 1rem; }
.panel h3 { margin: 0 0 0.5rem; font-size: 1rem; }
.panel code, code { background: #f1f5f9; padding: 0.1rem 0.35rem;
                    border-radius: 4px; font-size: 0.85rem; }
.cta { display: flex; gap: 0.75rem; margin-top: 1.25rem; flex-wrap: wrap; }
.cta a { background: var(--accent); color: #fff; text-decoration: none;
         padding: 0.55rem 1.1rem; border-radius: 6px; }
.cta a.secondary { background: #64748b; }
.steps { counter-reset: step; list-style: none; padding: 0; margin: 0.75rem 0 0; }
.steps li { position: relative; padding: 0 0 0.9rem 2.2rem; }
.steps li::before { counter-increment: step; content: counter(step);
                    position: absolute; left: 0; top: 0; width: 1.5rem; height: 1.5rem;
                    background: var(--accent); color: #fff; border-radius: 999px;
                    text-align: center; font-size: 0.85rem; line-height: 1.5rem; }
"""


def _layout(
    title: str,
    body: str,
    *,
    csrf: str | None = None,
    active: str | None = None,
    admin: bool = False,
) -> str:
    logout = ""
    if csrf is not None:
        logout = (
            f'<form method="post" action="/web/logout" style="margin-left:auto">'
            f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
            f'<button class="secondary">Logout</button></form>'
        )
    tenants_link = (
        '<a href="/web/tenants" class="active" '
        'aria-current="page">Tenants</a>'
        if admin and active == "tenants"
        else '<a href="/web/tenants">Tenants</a>'
        if admin
        else ""
    )
    nav = (
        '<div class="topbar">'
        '<a href="/web"><b>AgentSociety Hub</b></a>'
        f'<a href="/web"{" class=\"active\" aria-current=\"page\"" if active == "dashboard" else ""}>Dashboard</a>'
        f'<a href="/web/tasks"{" class=\"active\" aria-current=\"page\"" if active == "tasks" else ""}>Tasks</a>'
        f'<a href="/web/questions"{" class=\"active\" aria-current=\"page\"" if active == "questions" else ""}>Questions</a>'
        f'<a href="/web/contexts"{" class=\"active\" aria-current=\"page\"" if active == "contexts" else ""}>Consensus</a>'
        f'<a href="/web/directory"{" class=\"active\" aria-current=\"page\"" if active == "directory" else ""}>Directory</a>'
        f'<a href="/web/nodes"{" class=\"active\" aria-current=\"page\"" if active == "nodes" else ""}>Devices</a>'
        f'{tenants_link}'
        f'<a href="/web/account"{" class=\"active\" aria-current=\"page\"" if active == "account" else ""}>Account</a>'
        f"{logout}</div>"
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head>"
        f"<body>{nav}<main class=\"content\">{body}</main></body></html>"
    )


def landing_page(*, registration_open: bool = True) -> str:
    register_link = (
        '<a href="/web/register">Create account</a>'
        if registration_open
        else ""
    )
    nav = (
        '<div class="topbar">'
        '<a href="/web"><b>AgentSociety Hub</b></a>'
        '<a href="/web/login" style="margin-left:auto">Sign in</a>'
        f"{register_link}</div>"
    )
    body = (
        '<div class="hero">'
        "<h1>AgentSociety Hub</h1>"
        "<p>A shared coordination center for your agents. Register with your "
        "own account, connect your local agents, and delegate tasks between "
        "machines — securely, over TLS, with per-user isolation.</p>"
        '<span class="tag">password accounts · per-user isolation · REST / MCP / Web</span>'
        "</div>"
        "<h2>How it works</h2>"
        '<div class="grid">'
        '<div class="panel"><h3>1. Create your account</h3>'
        "<p>Sign up with a username and password. Your credentials are stored "
        "as a one-way hash; the web UI and API authenticate with the password "
        "only.</p></div>"
        '<div class="panel"><h3>2. Connect your agents</h3>'
        "<p>On each machine run <code>agent connect</code> and enter your "
        "username and password once. The Hub issues a per-node credential for "
        "that machine — your password never touches disk.</p></div>"
        '<div class="panel"><h3>3. Create and track tasks</h3>'
        "<p>Submit tasks from this dashboard, the REST API, or the Hub MCP "
        "tools (<code>hub_create_task</code>, <code>hub_list_tasks</code>, "
        "<code>hub_cancel_task</code>), then watch runs and artifacts.</p></div>"
        "</div>"
        '<div class="cta"><a href="/web/login">Sign in</a>'
        f"{'<a class=secondary href=\"/web/register\">Create account</a>' if registration_open else ''}"
        "</div>"
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>AgentSociety Hub</title><style>{_CSS}</style></head>"
        f"<body>{nav}<main class=\"content\">{body}</main></body></html>"
    )


def login_page(
    error: str | None = None, *, registration_open: bool = True
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    register_link = (
        '<p class="muted"><a href="/web/register">Create an account</a></p>'
        if registration_open
        else ""
    )
    body = (
        f"<h1>Hub login</h1>{error_html}"
        '<form method="post" action="/web/login">'
        '<label for="username">Username</label>'
        '<input id="username" name="username" type="text" required '
        'autocomplete="username" placeholder="your username">'
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" required '
        'autocomplete="current-password">'
        '<button type="submit">Sign in</button></form>'
        '<details><summary class="muted">Use an admin/token login instead</summary>'
        '<form method="post" action="/web/login">'
        '<label for="token">Hub API token</label>'
        '<input id="token" name="token" type="password" '
        'autocomplete="current-password" placeholder="bootstrap or tenant token">'
        '<button type="submit">Sign in with token</button></form></details>'
        f"{register_link}"
    )
    return _layout("Login", body)


def register_page(
    error: str | None = None,
    *,
    username: str = "",
    display_name: str = "",
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    body = (
        f"<h1>Create account</h1>{error_html}"
        '<form method="post" action="/web/register">'
        '<label for="username">Username</label>'
        '<input id="username" name="username" type="text" required '
        f'autocomplete="username" value="{_esc(username)}" '
        'placeholder="lowercase letters, digits, dot, dash, underscore">'
        '<label for="display_name">Display name</label>'
        '<input id="display_name" name="display_name" type="text" '
        f'value="{_esc(display_name)}">'
        '<label for="password">Password (at least 10 chars, letters + digits)</label>'
        '<input id="password" name="password" type="password" required '
        'autocomplete="new-password">'
        '<label for="password2">Repeat password</label>'
        '<input id="password2" name="password2" type="password" required '
        'autocomplete="new-password">'
        '<button type="submit">Register</button></form>'
        '<p class="muted"><a href="/web/login">Back to login</a></p>'
    )
    return _layout("Register", body)


def account_page(
    me: dict[str, Any],
    sessions: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    *,
    csrf: str,
    error: str | None = None,
    notice: str | None = None,
    admin: bool = False,
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    notice_html = (
        f'<div class="notice">{_esc(notice)}</div>' if notice else ""
    )
    account = me.get("account") or {}
    principal = me.get("principal") or {}
    session_rows = "".join(
        "<tr>"
        f"<td>{_esc(session.get('label'))}</td>"
        f"<td>{_esc(session.get('role'))}</td>"
        f'<td class="time" title="{_fmt(session.get("created_at"))}">'
        f"{_fmt_relative(session.get('created_at'))}</td>"
        f'<td class="time" title="{_fmt(session.get("expires_at"))}">'
        f"{_fmt_relative(session.get('expires_at'))}</td>"
        f"<td>{'revoked' if session.get('revoked_at') else 'active'}</td>"
        f"<td><form method=\"post\" action=\"/web/account/sessions/revoke\">"
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        f'<input type="hidden" name="session_token_id" '
        f'value="{_esc(session.get("session_token_id"))}">'
        '<button class="secondary" type="submit">Revoke</button></form></td>'
        "</tr>"
        for session in sessions
    ) or "<tr><td colspan=6 class=muted>No sessions</td></tr>"
    token_rows = "".join(
        "<tr>"
        f"<td>{_esc(token.get('label'))}</td>"
        f"<td>{_esc(token.get('role'))}</td>"
        f'<td class="short-id">{_esc(_short_id(token.get("node_id") or "-"))}</td>'
        f'<td class="time" title="{_fmt(token.get("created_at"))}">'
        f"{_fmt_relative(token.get('created_at'))}</td>"
        f'<td class="time" title="{_fmt(token.get("expires_at"))}">'
        f"{_fmt_relative(token.get('expires_at'))}</td>"
        f"<td>{'revoked' if token.get('revoked_at') else 'active'}</td>"
        f"<td><form method=\"post\" action=\"/web/account/tokens/revoke\">"
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        f'<input type="hidden" name="token_id" value="{_esc(token.get("token_id"))}">'
        '<button class="secondary" type="submit">Revoke</button></form></td>'
        "</tr>"
        for token in tokens
    ) or "<tr><td colspan=7 class=muted>No node credentials</td></tr>"
    body = (
        f"<h1>Account</h1>{error_html}{notice_html}"
        "<h2>Profile</h2>"
        "<table>"
        f"<tr><th>Username</th><td>{_esc(account.get('username') or '-')}</td></tr>"
        f"<tr><th>Display name</th><td>{_esc(account.get('display_name') or principal.get('display_name') or '-')}</td></tr>"
        f"<tr><th>Role</th><td>{_esc(me.get('role') or account.get('role') or '-')}</td></tr>"
        f"<tr><th>Tenant</th><td>{_esc(me.get('tenant_id') or account.get('tenant_id') or '-')}</td></tr>"
        f"<tr><th>Principal</th><td>{_esc(principal.get('principal_id') or '-')}</td></tr>"
        "</table>"
        "<h2>Change password</h2>"
        '<form method="post" action="/web/account/change-password">'
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        '<label for="old_password">Current password</label>'
        '<input id="old_password" name="old_password" type="password" required '
        'autocomplete="current-password">'
        '<label for="new_password">New password</label>'
        '<input id="new_password" name="new_password" type="password" required '
        'autocomplete="new-password">'
        '<button type="submit">Change password</button></form>'
        "<h2>Sessions</h2>"
        '<div class="table-wrap"><table><tr><th>Label</th><th>Role</th>'
        "<th>Created</th><th>Expires</th><th>Status</th><th></th></tr>"
        f"{session_rows}</table></div>"
        "<h2>Node credentials</h2>"
        '<div class="table-wrap"><table><tr><th>Label</th><th>Role</th>'
        "<th>Node</th><th>Created</th><th>Expires</th><th>Status</th>"
        f"<th></th></tr>{token_rows}</table></div>"
    )
    return _layout("Account", body, csrf=csrf, active="account", admin=admin)


def dashboard_page(
    stats: dict[str, int],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    user: dict[str, Any] | None = None,
    admin: bool = False,
    pending_questions: int = 0,
) -> str:
    if user:
        identity = (
            f"{_esc(user.get('username') or user.get('display_name') or '')} "
            f'<span class="pill">{_esc(user.get("role") or "")}</span>'
        )
    else:
        identity = '<span class="pill">admin</span>'
    welcome = (
        '<div class="hero"><h1>Dashboard</h1>'
        f"<p>Signed in as {identity}</p>"
        "<p>This view only shows data you are allowed to see: your own "
        "agents, tasks, runs, and artifacts.</p></div>"
    )
    active_tasks = int(stats.get("tasks_submitted", 0)) + int(
        stats.get("tasks_working", 0)
    )
    failed_tasks = int(stats.get("tasks_failed", 0)) + int(
        stats.get("tasks_cancelled", 0)
    )
    cards = (
        f'<div class="card"><b>{_esc(stats.get("nodes_online", 0))}'
        f' / {_esc(stats.get("nodes", 0))}</b>'
        "<span>Devices online</span></div>"
        f'<div class="card"><b>{_esc(active_tasks)}</b>'
        "<span>Active tasks</span></div>"
        f'<div class="card"><b>{_esc(pending_questions)}</b>'
        '<span><a href="/web/questions?status=pending">Pending questions</a></span></div>'
        f'<div class="card"><b>{_esc(stats.get("tasks_completed", 0))}</b>'
        "<span>Completed tasks</span></div>"
        f'<div class="card"><b>{_esc(failed_tasks)}</b>'
        "<span>Failed / cancelled</span></div>"
    )
    task_rows = "".join(
        f"<tr><td><a href=\"/web/tasks/{_esc(task['task_id'])}\">"
        f'<span class="short-id">{_esc(_short_id(task["task_id"]))}</span></a></td>'
        f"<td>{_status_pill(task['status'])}</td>"
        f"<td>{_esc(task['objective'][:120])}</td>"
        f'<td class="time" title="{_fmt(task["created_at"])}">'
        f"{_fmt_relative(task['created_at'])}</td></tr>"
        for task in tasks[:20]
    )
    if not task_rows:
        task_rows = (
            '<tr><td colspan="4" class="muted">No tasks yet. Create your '
            'first task below or from the Tasks page.</td></tr>'
        )
    run_rows = "".join(
        f'<tr><td class="short-id">{_esc(_short_id(run["run_id"]))}</td>'
        f'<td class="short-id">{_esc(_short_id(run.get("task_id") or "-"))}</td>'
        f"<td>{_status_pill(run['status'])}</td>"
        f"<td>{_esc(run['node_id'])}</td>"
        f'<td class="time" title="{_fmt(run["started_at"])}">'
        f"{_fmt_relative(run['started_at'])}</td></tr>"
        for run in runs[:10]
    )
    if not run_rows:
        run_rows = (
            '<tr><td colspan="5" class="muted">No runs yet. Runs appear '
            "once a task starts executing.</td></tr>"
        )
    body = (
        f"{welcome}"
        f'<div class="cards">{cards}</div>'
        '<div class="panel"><h3>Quick start</h3>'
        '<ol class="steps">'
        "<li>Sign in with your password account (or create one).</li>"
        "<li>On each agent machine run <code>./agent setup</code> then "
        "<code>./agent connect</code>, entering your username and password.</li>"
        "<li>Create a task from <a href=\"/web/tasks\">Tasks</a> or with "
        "<code>hub_create_task</code> (MCP); only your own agents appear in "
        "the assignee list.</li>"
        "<li>Watch progress under <a href=\"/web/runs\">Runs</a> and collect "
        "outputs under <a href=\"/web/artifacts\">Artifacts</a>.</li>"
        "</ol></div>"
        "<h2>Recent tasks</h2>"
        '<div class="table-wrap"><table><tr><th>Task</th><th>Status</th>'
        "<th>Objective</th><th>Created</th></tr>"
        f"{task_rows}</table></div>"
        "<h2>Recent runs</h2>"
        '<div class="table-wrap"><table><tr><th>Run</th><th>Task</th>'
        "<th>Status</th><th>Node</th><th>Started</th></tr>"
        f"{run_rows}</table></div>"
        '<p class="muted"><a href="/web/runs">View all runs</a> · '
        '<a href="/web/artifacts">View artifacts</a></p>'
    )
    return _layout("Dashboard", body, active="dashboard", admin=admin)


def tasks_page(
    tasks: list[dict[str, Any]],
    *,
    status_filter: str | None,
    principals: list[dict[str, Any]],
    actors: list[dict[str, Any]],
    csrf: str,
    error: str | None = None,
    admin: bool = False,
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    default_principal = principals[0]["principal_id"] if principals else ""
    default_actor = actors[0]["actor_id"] if actors else ""
    principal_options = "".join(
        f'<option value="{_esc(p["principal_id"])}"'
        f'{" selected" if p["principal_id"] == default_principal else ""}>'
        f'{_esc(p["principal_id"])}</option>'
        for p in principals
    )
    actor_options = "".join(
        f'<option value="{_esc(a["actor_id"])}"'
        f'{" selected" if a["actor_id"] == default_actor else ""}>'
        f'{_esc(a["actor_id"])}</option>'
        for a in actors
    )
    filters = "".join(
        (
            f'<a href="/web/tasks?status={status}"'
            f'{" style=\"font-weight:bold\"" if status_filter == status else ""}>'
            f"{status}</a>"
        )
        + "&nbsp;"
        for status in ("submitted", "working", "completed", "failed", "cancelled")
    )
    rows = "".join(
        f"<tr><td><a href=\"/web/tasks/{_esc(task['task_id'])}\">"
        f'<span class="short-id">{_esc(_short_id(task["task_id"]))}</span></a></td>'
        f"<td>{_status_pill(task['status'])}</td>"
        f"<td>{_esc(task['principal_id'])}</td>"
        f"<td>{_esc(task['objective'][:160])}</td>"
        f'<td class="time" title="{_fmt(task["created_at"])}">'
        f"{_fmt_relative(task['created_at'])}</td></tr>"
        for task in tasks
    )
    if not rows:
        rows = (
            '<tr><td colspan="5" class="muted">No tasks found. Create one '
            "below to get started.</td></tr>"
        )
    body = (
        "<h1>Tasks</h1>"
        f"{error_html}"
        '<p class="muted">Filter: <a href="/web/tasks"'
        f'{" style=\"font-weight:bold\"" if status_filter is None else ""}>all</a>&nbsp;'
        f"{filters}</p>"
        "<h2>Create task</h2>"
        '<form method="post" action="/web/tasks/create">'
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        "<label>What should the agent do?</label>"
        '<textarea name="objective" rows="4" required '
        'placeholder="Describe the task in plain language…"></textarea>'
        "<label>Assignee actor (optional)</label><select name=\"assignee_actor_id\">"
        '<option value="">(any capable node)</option>'
        f"{actor_options}</select>"
        '<details><summary>Advanced options</summary>'
        "<label>Principal</label><select name=\"principal_id\" required>"
        f"{principal_options}</select>"
        "<label>Delegator actor</label><select name=\"delegator_actor_id\" required>"
        f"{actor_options}</select>"
        "<label>Required capabilities (comma-separated)</label>"
        '<input name="required_capabilities" placeholder="code,pi">'
        "<label>Input JSON (optional)</label>"
        '<textarea name="input_json" rows="3" placeholder=\'{"workspace":"."}\'></textarea>'
        "<label>Idempotency key (optional)</label>"
        '<input name="idempotency_key">'
        "</details>"
        '<button type="submit">Create task</button></form>'
        "<h2>Task list</h2>"
        '<div class="table-wrap"><table><tr><th>Task</th><th>Status</th>'
        "<th>Principal</th><th>Objective</th><th>Created</th></tr>"
        f"{rows}</table></div>"
    )
    return _layout("Tasks", body, csrf=csrf, active="tasks", admin=admin)


def task_detail_page(
    task: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    csrf: str,
    admin: bool = False,
) -> str:
    artifacts = task.get("artifacts") or []
    artifact_rows = "".join(
        f'<tr><td class="short-id">{_esc(_short_id(a["artifact_id"]))}</td>'
        f"<td>{_esc(a['name'])}</td>"
        f"<td>{_esc(a['media_type'])}</td><td>{_esc(a['size_bytes'] or '-')}</td>"
        f'<td class="short-id">{_esc(_short_id(a["sha256"] or "-", 8))}</td>'
        f'<td class="time" title="{_fmt(a["created_at"])}">'
        f"{_fmt_relative(a['created_at'])}</td></tr>"
        for a in artifacts
    )
    if not artifact_rows:
        artifact_rows = (
            '<tr><td colspan="6" class="muted">No artifacts produced '
            "yet.</td></tr>"
        )
    event_rows = "".join(
        f"<tr><td>{_esc(event['seq'])}</td><td>{_esc(event['type'])}</td>"
        f'<td class="short-id">{_esc(_short_id(event.get("actor_id") or "-"))}</td>'
        f"<td>{_esc(event.get('message') or '-')}</td>"
        f'<td class="time" title="{_fmt(event["created_at"])}">'
        f"{_fmt_relative(event['created_at'])}</td></tr>"
        for event in events
    )
    if not event_rows:
        event_rows = (
            '<tr><td colspan="5" class="muted">No events yet. Progress '
            "appears here once a worker claims the task.</td></tr>"
        )
    run_rows = "".join(
        f'<tr><td class="short-id">{_esc(_short_id(run["run_id"]))}</td>'
        f"<td>{_esc(run['node_id'])}</td>"
        f"<td>{_status_pill(run['status'])}</td>"
        f'<td class="time" title="{_fmt(run["started_at"])}">'
        f"{_fmt_relative(run['started_at'])}</td></tr>"
        for run in runs
    )
    if not run_rows:
        run_rows = (
            '<tr><td colspan="4" class="muted">No runs yet.</td></tr>'
        )
    result_text = (task.get("result") or {}).get("text") or ""
    result_panel = (
        '<div class="panel"><h3>Result</h3>'
        f'<p style="white-space:pre-wrap">{_esc(result_text)}</p></div>'
        if result_text
        else ""
    )
    partial_events = [
        event for event in events if event["type"] == "task.partial_result"
    ]
    progress_panel = ""
    if partial_events:
        latest = partial_events[-1]
        progress = (latest.get("payload") or {}).get("partial_result") or {}
        fields = "".join(
            f"<tr><th>{_esc(name)}</th><td>{_esc(str(progress.get(name) or '-'))}</td></tr>"
            for name in ("phase", "toolCount", "messageCount", "lastTool")
            if name in progress
        )
        progress_panel = (
            '<div class="panel"><h3>Live progress</h3>'
            f'<table style="max-width:420px">{fields}</table>'
            f'<p class="muted">Reported {_fmt_relative(latest.get("created_at"))} '
            f'(<span class="time" title="{_fmt(latest.get("created_at"))}">'
            f"{_fmt(latest.get('created_at'))}</span>)</p></div>"
        )
    elif task["status"] == "working":
        progress_panel = (
            '<div class="panel"><h3>Live progress</h3>'
            '<p class="muted">The worker has not reported progress yet.</p></div>'
        )
    cancel_form = ""
    if task["status"] not in {"completed", "failed", "cancelled"}:
        cancel_form = (
            '<form method="post" '
            f'action="/web/tasks/{_esc(task["task_id"])}/cancel">'
            f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
            '<input type="hidden" name="actor_id" '
            f'value="{_esc(task["delegator_actor_id"])}">'
            '<label>Cancel reason (optional)</label>'
            '<input name="reason" maxlength="10000">'
            '<button class="secondary" type="submit">Cancel task</button></form>'
        )
    body = (
        f"<h1>Task <span class=\"short-id\">{_esc(_short_id(task['task_id'], 16))}</span></h1>"
        f"<p><a href=\"/web/tasks\">&larr; Back to tasks</a></p>"
        "<table><tr><th>Status</th><td>"
        f"{_status_pill(task['status'])}</td></tr>"
        f"<tr><th>Principal</th><td>{_esc(task['principal_id'])}</td></tr>"
        f"<tr><th>Delegator</th><td>{_esc(task['delegator_actor_id'])}</td></tr>"
        f"<tr><th>Assignee</th><td>{_esc(task.get('assignee_actor_id') or '-')}</td></tr>"
        f'<tr><th>Created</th><td class="time" title="{_fmt(task["created_at"])}">'
        f"{_fmt_relative(task['created_at'])}</td></tr>"
        f'<tr><th>Updated</th><td class="time" title="{_fmt(task["updated_at"])}">'
        f"{_fmt_relative(task['updated_at'])}</td></tr>"
        f"<tr><th>Objective</th><td>{_esc(task['objective'])}</td></tr>"
        f"<tr><th>Error</th><td>{_esc(task.get('error') or '-')}</td></tr>"
        f"<tr><th>Attempts</th><td>{_esc(task['attempts'])}</td></tr></table>"
        f"{progress_panel}"
        f"{result_panel}"
        "<details><summary>Raw input / result JSON</summary>"
        f"<h3>Input</h3><pre>{_json_text(task['input'])}</pre>"
        f"<h3>Result</h3><pre>{_json_text(task['result'])}</pre></details>"
        f"{cancel_form}"
        "<h2>Events</h2>"
        '<div class="table-wrap"><table><tr><th>Seq</th><th>Type</th>'
        "<th>Actor</th><th>Message</th><th>Time</th></tr>"
        f"{event_rows}</table></div>"
        "<h2>Runs</h2>"
        '<div class="table-wrap"><table><tr><th>Run</th><th>Node</th>'
        "<th>Status</th><th>Started</th></tr>"
        f"{run_rows}</table></div>"
        "<h2>Artifacts</h2>"
        '<div class="table-wrap"><table><tr><th>Artifact</th><th>Name</th>'
        "<th>Type</th><th>Size</th><th>SHA-256</th><th>Created</th></tr>"
        f"{artifact_rows}</table></div>"
    )
    return _layout(
        f"Task {task['task_id']}", body, csrf=csrf, active="tasks", admin=admin
    )


def questions_page(
    questions: list[dict[str, Any]],
    *,
    status_filter: str | None,
    csrf: str | None = None,
    admin: bool = False,
    error: str | None = None,
) -> str:
    filters = ["all", "pending", "claimed", "answered", "expired", "unsupported", "declined"]
    links = []
    for name in filters:
        if name == "all":
            href = "/web/questions"
            label = "All"
        else:
            href = f"/web/questions?status={name}"
            label = name.capitalize()
        cls = ' class="active" aria-current="page"' if name == (status_filter or "all") else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    rows = ""
    for question in questions:
        qid = str(question["question_id"])
        status = str(question["status"])
        message = _esc(str(question.get("message") or "")[:200])
        actions = ""
        if status == "pending" and csrf:
            actions = (
                f'<form method="post" action="/web/questions/{_esc(qid)}/answer" style="margin:0">'
                f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
                '<label>Your answer</label>'
                '<textarea name="answer_text" rows="2" maxlength="50000" required></textarea>'
                '<button type="submit">Answer</button></form>'
                f'<form method="post" action="/web/questions/{_esc(qid)}/decline" style="margin:0">'
                f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
                '<input name="reason" maxlength="10000" placeholder="reason (optional)">'
                '<button class="secondary" type="submit">Decline</button></form>'
            )
        elif status == "claimed":
            actions = '<span class="muted">Worker is answering…</span>'
        elif status == "answered":
            actions = (
                '<p style="white-space:pre-wrap">'
                f"{_esc(str(question.get('answer_text') or ''))}</p>"
            )
        elif status == "declined":
            actions = '<span class="muted">Declined by human</span>'
        elif status == "expired":
            actions = '<span class="muted">No answer within the TTL</span>'
        elif status == "unsupported":
            actions = '<span class="muted">Target has no online node</span>'
        rows += (
            "<tr>"
            f'<td class="short-id">{_esc(_short_id(qid))}</td>'
            f"<td>{_status_pill(status)}</td>"
            f'<td class="short-id">{_esc(_short_id(question.get("asker_actor_id") or "-"))}</td>'
            f'<td class="short-id">{_esc(_short_id(question.get("target_actor_id") or "-"))}</td>'
            f"<td>{message}</td>"
            f'<td class="time" title="{_fmt(question.get("created_at"))}">'
            f"{_fmt_relative(question.get('created_at'))}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    if not rows:
        rows = (
            '<tr><td colspan="7" class="muted">No questions. Agents ask '
            "questions through <code>hub_ask</code> while working; pending "
            'ones can be answered right here.</td></tr>'
        )
    body = (
        "<h1>Questions</h1>"
        '<p class="muted">Questions are asked by agents during tasks '
        "(<code>hub_ask</code>). A pending question can be answered or "
        "declined by a human from this page; the asking agent receives the "
        "answer in its current turn.</p>"
        f'{"<div class=\"error\">" + _esc(error) + "</div>" if error else ""}'
        f'<div class="panel" style="display:flex;gap:1rem;flex-wrap:wrap">{ "".join(links) }</div>'
        '<div class="table-wrap"><table><tr><th>Question</th><th>Status</th>'
        "<th>Asker</th><th>Target</th><th>Message</th><th>Created</th>"
        "<th>Answer / action</th></tr>"
        f"{rows}</table></div>"
    )
    return _layout("Questions", body, csrf=csrf, active="questions", admin=admin)


def contexts_page(
    events: list[dict[str, Any]],
    *,
    scope_filter: str | None,
    order: str = "desc",
    admin: bool = False,
) -> str:
    def summary_of(event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        for key in ("summary", "title", "result", "answer"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().replace("\n", " ")[:160]
        return ""

    filters = ["consensus", "qa", "directory", "all"]
    links = []
    for name in filters:
        if name == "all":
            href = "/web/contexts?scope=all"
            label = "All"
        else:
            href = f"/web/contexts?scope={name}"
            label = name.capitalize()
        cls = ' class="active" aria-current="page"' if name == (scope_filter or "consensus") else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    rows = ""
    for event in events:
        payload = event.get("payload") or {}
        rows += (
            "<tr>"
            f"<td>{_esc(event['seq'])}</td>"
            f"<td>{_esc(event['kind'])}</td>"
            f'<td class="short-id">{_esc(_short_id(event.get("session_id") or "-"))}</td>'
            f"<td>{_esc(summary_of(event))}</td>"
            f'<td class="time" title="{_fmt(event.get("created_at"))}">'
            f"{_fmt_relative(event.get('created_at'))}</td>"
            "<td><details><summary>payload</summary>"
            f"<pre>{_json_text(payload)}</pre></details></td>"
            "</tr>"
        )
    if not rows:
        rows = (
            '<tr><td colspan="6" class="muted">No shared memory entries yet. '
            "Workers append digests with <code>AGENT_SOCIETY_CONTEXT=1</code>; "
            "question answers appear here automatically.</td></tr>"
        )
    order_links = []
    for name, label in (("desc", "Newest first"), ("asc", "Oldest first")):
        href = f"/web/contexts?scope={quote(scope_filter or 'consensus')}&order={name}"
        cls = ' class="active" aria-current="true"' if name == order else ""
        order_links.append(f'<a href="{href}"{cls}>{label}</a>')
    body = (
        "<h1>Consensus context</h1>"
        '<p class="muted">The principal shared memory: session digests, '
        "facts, decisions, and question answers. Entries expire after their "
        "TTL; the log is append-only.</p>"
        f'<div class="panel" style="display:flex;gap:1rem;flex-wrap:wrap">{ "".join(links) }</div>'
        f'<div class="panel" style="display:flex;gap:1rem;align-items:center">'
        f'<span class="muted">Sort:</span>{"".join(order_links)}</div>'
        '<div class="table-wrap"><table><tr><th>Seq</th><th>Kind</th>'
        "<th>Session</th><th>Summary</th><th>Time</th><th></th></tr>"
        f"{rows}</table></div>"
    )
    return _layout("Consensus", body, active="contexts", admin=admin)


def directory_page(rows: list[dict[str, Any]], *, admin: bool = False) -> str:
    table_rows = ""
    for row in rows:
        payload = row.get("payload") or {}
        session_id = str(row.get("session_id") or "-")
        invocations = payload.get("invocations") or []
        table_rows += (
            "<tr>"
            f'<td class="short-id"><a href="/web/directory/{_esc(session_id)}">'
            f"{_esc(_short_id(session_id))}</a></td>"
            f'<td class="short-id">{_esc(_short_id(row.get("actor_id") or "-"))}</td>'
            f'<td>{_display_title(payload)}</td>'
            f"<td class=\"short-id\">{_esc(str(payload.get('workspace') or '-'))}</td>"
            f"<td>{_status_pill(payload.get('status') or 'idle')}</td>"
            f'<td class="time" title="{_fmt(payload.get("last_active_at"))}">'
            f"{_fmt_relative(payload.get('last_active_at'))}</td>"
            f"<td>{len(invocations)}</td>"
            "</tr>"
        )
    if not table_rows:
        table_rows = (
            '<tr><td colspan="7" class="muted">No sessions in the directory '
            "yet. Devices push rows through the AgentSociety bundle; remote "
            "sessions appear here within the sync interval.</td></tr>"
        )
    body = (
        "<h1>Session directory</h1>"
        '<p class="muted">One row per session across your devices: identity, '
        "workspace, status, and invocation history. Click a session for its "
        "consensus digests and artifacts.</p>"
        '<div class="table-wrap"><table><tr><th>Session</th><th>Actor</th>'
        "<th>Title</th><th>Workspace</th><th>Status</th>"
        "<th>Last active</th><th>Invocations</th></tr>"
        f"{table_rows}</table></div>"
    )
    return _layout("Directory", body, active="directory", admin=admin)


def directory_detail_page(row: dict[str, Any], *, admin: bool = False) -> str:
    payload = row.get("payload") or {}
    session_id = str(row.get("session_id") or "-")
    consensus = row.get("consensus") or []
    artifacts = row.get("artifacts") or []
    consensus_rows = "".join(
        f"<tr><td>{_esc(event['seq'])}</td><td>{_esc(event['kind'])}</td>"
        f"<td>{_esc(str((event.get('payload') or {}).get('summary') or (event.get('payload') or {}).get('answer') or ''))}</td>"
        f'<td class="time" title="{_fmt(event.get("created_at"))}">'
        f"{_fmt_relative(event.get('created_at'))}</td></tr>"
        for event in consensus
    )
    if not consensus_rows:
        consensus_rows = '<tr><td colspan="4" class="muted">No consensus entries for this session.</td></tr>'
    artifact_rows = "".join(
        f"<tr><td>{_esc(a['name'])}</td><td>{_esc(a['media_type'])}</td>"
        f'<td class="short-id">{_esc(a.get("uri") or "-")}</td>'
        f'<td class="short-id">{_esc(a.get("task_id") or a.get("run_id") or "-")}</td></tr>'
        for a in artifacts
    )
    if not artifact_rows:
        artifact_rows = '<tr><td colspan="4" class="muted">No artifacts recorded for this session.</td></tr>'
    body = (
        f"<h1>Session <span class=\"short-id\">{_esc(session_id)}</span></h1>"
        f'<p><a href="/web/directory">&larr; Back to directory</a></p>'
        "<table>"
        f"<tr><th>Actor</th><td>{_esc(row.get('actor_id') or '-')}</td></tr>"
        f"<tr><th>Node</th><td>{_esc(row.get('node_id') or '-')}</td></tr>"
        f"<tr><th>Title</th><td>{_display_title(payload)}</td></tr>"
        f"<tr><th>Workspace</th><td>{_esc(str(payload.get('workspace') or '-'))}</td></tr>"
        f"<tr><th>Status</th><td>{_status_pill(payload.get('status') or 'idle')}</td></tr>"
        f"<tr><th>Session mode</th><td>{_esc(str(payload.get('session_mode') or '-'))}</td></tr>"
        f"<tr><th>Tool policy</th><td>{_esc(str(payload.get('tool_policy') or '-'))}</td></tr>"
        f'<tr><th>Last active</th><td class="time" title="{_fmt(payload.get("last_active_at"))}">'
        f"{_fmt_relative(payload.get('last_active_at'))}</td></tr>"
        "</table>"
        "<h2>Invocations</h2>"
        '<div class="table-wrap"><table><tr><th>Run</th><th>Status</th>'
        "<th>Objective</th><th>At</th></tr>"
        + "".join(
            f'<tr><td class="short-id">{_esc(_short_id(inv.get("run_id") or "-"))}</td>'
            f"<td>{_status_pill(inv.get('status') or '-')}</td>"
            f"<td>{_esc(str(inv.get('objective') or '')[:120])}</td>"
            f'<td class="time" title="{_fmt(inv.get("at"))}">'
            f"{_fmt_relative(inv.get('at'))}</td></tr>"
            for inv in (payload.get("invocations") or [])
        )
        + "</table></div>"
        "<h2>Consensus</h2>"
        '<div class="table-wrap"><table><tr><th>Seq</th><th>Kind</th>'
        "<th>Summary</th><th>Time</th></tr>"
        f"{consensus_rows}</table></div>"
        "<h2>Artifacts</h2>"
        '<div class="table-wrap"><table><tr><th>Name</th><th>Type</th>'
        "<th>URI</th><th>Task/Run</th></tr>"
        f"{artifact_rows}</table></div>"
    )
    return _layout(
        f"Session {session_id}", body, active="directory", admin=admin
    )


def nodes_page(
    principals: list[dict[str, Any]],
    actors: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    *,
    admin: bool = False,
) -> str:
    principal_rows = "".join(
        f'<tr><td class="short-id">{_esc(p["principal_id"])}</td>'
        f"<td>{_esc(p['kind'])}</td>"
        f"<td>{_esc(p['display_name'])}</td></tr>"
        for p in principals
    )
    actor_rows = "".join(
        f'<tr><td class="short-id">{_esc(a["actor_id"])}</td>'
        f'<td class="short-id">{_esc(a["principal_id"])}</td>'
        f"<td>{_esc(a['kind'])}</td><td>{_esc(', '.join(a['capabilities']))}</td></tr>"
        for a in actors
    )
    node_rows = "".join(
        f'<tr><td class="short-id">{_esc(n["node_id"])}</td>'
        f'<td class="short-id">{_esc(n["actor_id"])}</td>'
        f"<td>{_esc(n['display_name'])}</td>"
        f"<td><span class=\"pill status-{_esc(n['status'])}\">"
        f"{_esc(n['status'])}</span></td>"
        f'<td class="time" title="{_fmt(n["last_seen_at"])}">'
        f"{_fmt_relative(n['last_seen_at'])}</td></tr>"
        for n in nodes
    )
    if not node_rows:
        node_rows = (
            '<tr><td colspan="5" class="muted">No devices connected yet. '
            'Run <code>agent connect</code> on a machine to add one.</td></tr>'
        )
    body = (
        "<h1>Devices</h1>"
        '<div class="table-wrap"><table><tr><th>Node</th><th>Actor</th>'
        "<th>Name</th><th>Status</th><th>Last seen</th></tr>"
        f"{node_rows}</table></div>"
        "<details><summary>Identities (advanced)</summary>"
        "<h2>Principals</h2>"
        '<div class="table-wrap"><table><tr><th>Principal</th><th>Kind</th>'
        f"<th>Name</th></tr>{principal_rows}</table></div>"
        "<h2>Actors</h2>"
        '<div class="table-wrap"><table><tr><th>Actor</th><th>Principal</th>'
        "<th>Kind</th><th>Capabilities</th></tr>"
        f"{actor_rows}</table></div></details>"
    )
    return _layout("Devices", body, active="nodes", admin=admin)


def runs_page(runs: list[dict[str, Any]], *, admin: bool = False) -> str:
    rows = "".join(
        f'<tr><td class="short-id">{_esc(_short_id(run["run_id"]))}</td>'
        f'<td class="short-id">{_esc(_short_id(run.get("task_id") or "-"))}</td>'
        f"<td>{_esc(run['principal_id'])}</td><td>{_esc(run['node_id'])}</td>"
        f"<td>{_status_pill(run['status'])}</td>"
        f'<td class="time" title="{_fmt(run["started_at"])}">'
        f"{_fmt_relative(run['started_at'])}</td>"
        f'<td class="time" title="{_fmt(run.get("completed_at"))}">'
        f"{_fmt_relative(run.get('completed_at'))}</td></tr>"
        for run in runs
    )
    if not rows:
        rows = (
            '<tr><td colspan="7" class="muted">No runs yet. Runs appear '
            "once a worker starts a task.</td></tr>"
        )
    body = (
        "<h1>Runs</h1>"
        '<div class="table-wrap"><table><tr><th>Run</th><th>Task</th>'
        "<th>Principal</th><th>Node</th><th>Status</th><th>Started</th>"
        f"<th>Completed</th></tr>{rows}</table></div>"
    )
    return _layout("Runs", body, admin=admin)


def artifacts_page(artifacts: list[dict[str, Any]], *, admin: bool = False) -> str:
    rows = "".join(
        f'<tr><td class="short-id">{_esc(_short_id(a["artifact_id"]))}</td>'
        f"<td>{_esc(a['name'])}</td>"
        f'<td class="short-id">{_esc(_short_id(a.get("task_id") or "-"))}</td>'
        f'<td class="short-id">{_esc(_short_id(a.get("run_id") or "-"))}</td>'
        f"<td>{_esc(a['media_type'])}</td><td>{_esc(a['size_bytes'] or '-')}</td>"
        f'<td class="time" title="{_fmt(a["created_at"])}">'
        f"{_fmt_relative(a['created_at'])}</td></tr>"
        for a in artifacts
    )
    if not rows:
        rows = (
            '<tr><td colspan="7" class="muted">No artifacts yet. Artifacts '
            "appear when a task uploads files or objects.</td></tr>"
        )
    body = (
        "<h1>Artifacts</h1>"
        '<div class="table-wrap"><table><tr><th>Artifact</th><th>Name</th>'
        "<th>Task</th><th>Run</th><th>Type</th><th>Size</th><th>Created</th></tr>"
        f"{rows}</table></div>"
    )
    return _layout("Artifacts", body, admin=admin)


def tenants_page(
    tenants: list[dict[str, Any]],
    *,
    csrf: str,
    error: str | None = None,
    admin: bool = True,
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    rows = "".join(
        f"<tr><td><a href=\"/web/tenants/{_esc(t['tenant_id'])}\">"
        f'{_esc(_short_id(t["tenant_id"], 20))}</a></td>'
        f"<td>{_esc(t['display_name'])}</td>"
        f'<td class="time" title="{_fmt(t["created_at"])}">'
        f"{_fmt_relative(t['created_at'])}</td></tr>"
        for t in tenants
    )
    if not rows:
        rows = '<tr><td colspan="3" class="muted">No tenants.</td></tr>'
    body = (
        "<h1>Tenants</h1>"
        f"{error_html}"
        "<h2>Create tenant</h2>"
        '<form method="post" action="/web/tenants/create">'
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        "<label>Tenant ID</label><input name=\"tenant_id\" required>"
        "<label>Display name</label><input name=\"display_name\" required>"
        '<button type="submit">Create tenant</button></form>'
        "<h2>Tenant list</h2>"
        '<div class="table-wrap"><table><tr><th>Tenant</th><th>Name</th>'
        f"<th>Created</th></tr>{rows}</table></div>"
    )
    return _layout("Tenants", body, csrf=csrf, active="tenants", admin=admin)


def tenant_detail_page(
    tenant: dict[str, Any],
    *,
    tokens: list[dict[str, Any]],
    principals: list[dict[str, Any]],
    actors: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    csrf: str,
    error: str | None = None,
    created_raw_token: str | None = None,
    admin: bool = True,
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    raw_html = ""
    if created_raw_token:
        raw_html = (
            '<div class="error" style="background:#ecfdf5;color:#065f46;'
            'border-color:#a7f3d0">Created token (copy now, shown once): '
            f"<code>{_esc(created_raw_token)}</code></div>"
        )
    principal_options = "".join(
        f'<option value="{_esc(p["principal_id"])}">{_esc(p["principal_id"])}</option>'
        for p in principals
    )
    actor_options = "".join(
        f'<option value="{_esc(a["actor_id"])}">{_esc(a["actor_id"])}</option>'
        for a in actors
    )
    node_options = "".join(
        f'<option value="{_esc(n["node_id"])}">{_esc(n["node_id"])}</option>'
        for n in nodes
    )
    token_rows = "".join(
        f'<tr><td class="short-id">{_esc(_short_id(t["token_id"]))}</td>'
        f"<td>{_esc(t['role'])}</td>"
        f"<td>{_esc(t['label'])}</td><td>{_esc(t.get('actor_id') or '-')}</td>"
        f"<td>{_esc(t.get('node_id') or '-')}</td>"
        f'<td class="time" title="{_fmt(t["created_at"])}">'
        f"{_fmt_relative(t['created_at'])}</td>"
        f"<td>{'revoked' if t.get('revoked_at') else 'active'}</td>"
        "<td>"
        + (
            f'<form method="post" action="/web/tokens/{_esc(t["token_id"])}/revoke">'
            f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
            '<button class="secondary" type="submit">Revoke</button></form>'
            if t.get("revoked_at") is None
            else "-"
        )
        + "</td></tr>"
        for t in tokens
    )
    if not token_rows:
        token_rows = '<tr><td colspan="8" class="muted">No tokens.</td></tr>'
    body = (
        f"<h1>Tenant {_esc(tenant['tenant_id'])}</h1>"
        f"<p><a href=\"/web/tenants\">&larr; Back to tenants</a></p>"
        f"<p><b>{_esc(tenant['display_name'])}</b></p>"
        f"{error_html}{raw_html}"
        "<h2>Issue token</h2>"
        f'<form method="post" action="/web/tenants/{_esc(tenant["tenant_id"])}/tokens/create">'
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        "<label>Role</label><select name=\"role\">"
        "<option value=\"tenant_admin\">tenant_admin</option>"
        "<option value=\"tenant_user\">tenant_user</option>"
        "<option value=\"node\">node</option></select>"
        "<label>Label</label><input name=\"label\" required>"
        "<label>Principal (for tenant_admin/tenant_user)</label>"
        f"<select name=\"principal_id\"><option value=\"\">-</option>"
        f"{principal_options}</select>"
        "<label>Actor (required for node)</label>"
        f"<select name=\"actor_id\"><option value=\"\">-</option>{actor_options}</select>"
        "<label>Node (required for node)</label>"
        f"<select name=\"node_id\"><option value=\"\">-</option>{node_options}</select>"
        '<button type="submit">Create token</button></form>'
        "<h2>Tokens</h2>"
        '<div class="table-wrap"><table><tr><th>Token</th><th>Role</th>'
        "<th>Label</th><th>Actor</th><th>Node</th><th>Created</th>"
        f"<th>Status</th><th>Action</th></tr>"
        f"{token_rows}</table></div>"
    )
    return _layout(
        f"Tenant {tenant['tenant_id']}",
        body,
        csrf=csrf,
        active="tenants",
        admin=admin,
    )


def not_found_page() -> str:
    return _layout("Not found", "<h1>404</h1><p>Page not found.</p>")
