from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from typing import Any


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value))


def _fmt(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(float(timestamp), timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
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
.topbar form { margin: 0; }
.content { max-width: 1100px; margin: 1.5rem auto; padding: 0 1.25rem; }
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
.muted { color: #64748b; font-size: 0.85rem; }
"""


def _layout(title: str, body: str, *, csrf: str | None = None) -> str:
    logout = ""
    if csrf is not None:
        logout = (
            f'<form method="post" action="/web/logout" style="margin-left:auto">'
            f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
            f'<button class="secondary">Logout</button></form>'
        )
    nav = (
        '<div class="topbar">'
        '<a href="/web"><b>AgentSociety Hub</b></a>'
        '<a href="/web/tasks">Tasks</a>'
        '<a href="/web/runs">Runs</a>'
        '<a href="/web/artifacts">Artifacts</a>'
        '<a href="/web/nodes">Nodes &amp; Identities</a>'
        '<a href="/web/tenants">Tenants</a>'
        f"{logout}</div>"
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head>"
        f"<body>{nav}<main class=\"content\">{body}</main></body></html>"
    )


def login_page(error: str | None = None) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    body = (
        f"<h1>Hub login</h1>{error_html}"
        '<form method="post" action="/web/login">'
        '<label for="token">Hub API token</label>'
        '<input id="token" name="token" type="password" required '
        'autocomplete="current-password" placeholder="bootstrap or tenant token">'
        '<button type="submit">Sign in</button></form>'
    )
    return _layout("Login", body)


def dashboard_page(
    stats: dict[str, int], tasks: list[dict[str, Any]], runs: list[dict[str, Any]]
) -> str:
    cards = "".join(
        f'<div class="card"><b>{_esc(stats.get(key, 0))}</b>'
        f"<span>{_esc(label)}</span></div>"
        for key, label in (
            ("principals", "Principals"),
            ("actors", "Actors"),
            ("nodes", "Nodes"),
            ("tasks", "Tasks"),
            ("runs", "Runs"),
            ("artifacts", "Artifacts"),
        )
    )
    task_rows = "".join(
        f"<tr><td><a href=\"/web/tasks/{_esc(task['task_id'])}\">"
        f"{_esc(task['task_id'])}</a></td>"
        f"<td><span class=\"pill\">{_esc(task['status'])}</span></td>"
        f"<td>{_esc(task['objective'][:120])}</td>"
        f"<td>{_fmt(task['created_at'])}</td></tr>"
        for task in tasks[:20]
    )
    if not task_rows:
        task_rows = '<tr><td colspan="4" class="muted">No tasks yet.</td></tr>'
    run_rows = "".join(
        f"<tr><td>{_esc(run['run_id'])}</td>"
        f"<td>{_esc(run.get('task_id') or '-')}</td>"
        f"<td><span class=\"pill\">{_esc(run['status'])}</span></td>"
        f"<td>{_esc(run['node_id'])}</td>"
        f"<td>{_fmt(run['started_at'])}</td></tr>"
        for run in runs[:10]
    )
    if not run_rows:
        run_rows = '<tr><td colspan="5" class="muted">No runs yet.</td></tr>'
    body = (
        "<h1>Dashboard</h1>"
        f'<div class="cards">{cards}</div>'
        "<h2>Recent tasks</h2>"
        "<table><tr><th>Task</th><th>Status</th><th>Objective</th><th>Created</th></tr>"
        f"{task_rows}</table>"
        "<h2>Recent runs</h2>"
        "<table><tr><th>Run</th><th>Task</th><th>Status</th><th>Node</th>"
        f"<th>Started</th></tr>{run_rows}</table>"
    )
    return _layout("Dashboard", body)


def tasks_page(
    tasks: list[dict[str, Any]],
    *,
    status_filter: str | None,
    principals: list[dict[str, Any]],
    actors: list[dict[str, Any]],
    csrf: str,
    error: str | None = None,
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    principal_options = "".join(
        f'<option value="{_esc(p["principal_id"])}">{_esc(p["principal_id"])}</option>'
        for p in principals
    )
    actor_options = "".join(
        f'<option value="{_esc(a["actor_id"])}">{_esc(a["actor_id"])}</option>'
        for a in actors
    )
    filters = "".join(
        f'<a href="/web/tasks?status={status}">{status}</a>&nbsp;'
        for status in ("submitted", "working", "completed", "failed", "cancelled")
    )
    rows = "".join(
        f"<tr><td><a href=\"/web/tasks/{_esc(task['task_id'])}\">"
        f"{_esc(task['task_id'])}</a></td>"
        f"<td><span class=\"pill\">{_esc(task['status'])}</span></td>"
        f"<td>{_esc(task['principal_id'])}</td>"
        f"<td>{_esc(task['objective'][:160])}</td>"
        f"<td>{_fmt(task['created_at'])}</td></tr>"
        for task in tasks
    )
    if not rows:
        rows = '<tr><td colspan="5" class="muted">No tasks found.</td></tr>'
    body = (
        "<h1>Tasks</h1>"
        f"{error_html}"
        "<p class=\"muted\">Filter: <a href=\"/web/tasks\">all</a>&nbsp;"
        f"{filters}</p>"
        "<h2>Create task</h2>"
        '<form method="post" action="/web/tasks/create">'
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">'
        "<label>Principal</label><select name=\"principal_id\" required>"
        f"{principal_options}</select>"
        "<label>Delegator actor</label><select name=\"delegator_actor_id\" required>"
        f"{actor_options}</select>"
        "<label>Assignee actor (optional)</label><select name=\"assignee_actor_id\">"
        '<option value="">(any capable node)</option>'
        f"{actor_options}</select>"
        "<label>Objective</label><textarea name=\"objective\" rows=\"4\" required>"
        "</textarea>"
        "<label>Required capabilities (comma-separated)</label>"
        '<input name="required_capabilities" placeholder="code,pi">'
        "<label>Input JSON (optional)</label>"
        '<textarea name="input_json" rows="3" placeholder=\'{"workspace":"."}\'></textarea>'
        "<label>Idempotency key (optional)</label>"
        '<input name="idempotency_key">'
        '<button type="submit">Create task</button></form>'
        "<h2>Task list</h2>"
        "<table><tr><th>Task</th><th>Status</th><th>Principal</th>"
        f"<th>Objective</th><th>Created</th></tr>{rows}</table>"
    )
    return _layout("Tasks", body, csrf=csrf)


def task_detail_page(
    task: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    csrf: str,
) -> str:
    artifacts = task.get("artifacts") or []
    artifact_rows = "".join(
        f"<tr><td>{_esc(a['artifact_id'])}</td><td>{_esc(a['name'])}</td>"
        f"<td>{_esc(a['media_type'])}</td><td>{_esc(a['size_bytes'] or '-')}</td>"
        f"<td>{_esc(a['sha256'] or '-')}</td><td>{_fmt(a['created_at'])}</td></tr>"
        for a in artifacts
    )
    if not artifact_rows:
        artifact_rows = '<tr><td colspan="6" class="muted">No artifacts.</td></tr>'
    event_rows = "".join(
        f"<tr><td>{_esc(event['seq'])}</td><td>{_esc(event['type'])}</td>"
        f"<td>{_esc(event.get('actor_id') or '-')}</td>"
        f"<td>{_esc(event.get('message') or '-')}</td>"
        f"<td>{_fmt(event['created_at'])}</td></tr>"
        for event in events
    )
    if not event_rows:
        event_rows = '<tr><td colspan="5" class="muted">No events.</td></tr>'
    run_rows = "".join(
        f"<tr><td>{_esc(run['run_id'])}</td><td>{_esc(run['node_id'])}</td>"
        f"<td><span class=\"pill\">{_esc(run['status'])}</span></td>"
        f"<td>{_fmt(run['started_at'])}</td></tr>"
        for run in runs
    )
    if not run_rows:
        run_rows = '<tr><td colspan="4" class="muted">No runs.</td></tr>'
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
        f"<h1>Task {_esc(task['task_id'])}</h1>"
        f"<p><a href=\"/web/tasks\">&larr; Back to tasks</a></p>"
        "<table><tr><th>Status</th><td><span class=\"pill\">"
        f"{_esc(task['status'])}</span></td></tr>"
        f"<tr><th>Principal</th><td>{_esc(task['principal_id'])}</td></tr>"
        f"<tr><th>Delegator</th><td>{_esc(task['delegator_actor_id'])}</td></tr>"
        f"<tr><th>Assignee</th><td>{_esc(task.get('assignee_actor_id') or '-')}</td></tr>"
        f"<tr><th>Created</th><td>{_fmt(task['created_at'])}</td></tr>"
        f"<tr><th>Updated</th><td>{_fmt(task['updated_at'])}</td></tr>"
        f"<tr><th>Objective</th><td>{_esc(task['objective'])}</td></tr>"
        f"<tr><th>Input</th><td><pre>{_json_text(task['input'])}</pre></td></tr>"
        f"<tr><th>Result</th><td><pre>{_json_text(task['result'])}</pre></td></tr>"
        f"<tr><th>Error</th><td>{_esc(task.get('error') or '-')}</td></tr>"
        f"<tr><th>Attempts</th><td>{_esc(task['attempts'])}</td></tr></table>"
        f"{cancel_form}"
        "<h2>Events</h2>"
        "<table><tr><th>Seq</th><th>Type</th><th>Actor</th><th>Message</th>"
        f"<th>Time</th></tr>{event_rows}</table>"
        "<h2>Runs</h2>"
        "<table><tr><th>Run</th><th>Node</th><th>Status</th><th>Started</th></tr>"
        f"{run_rows}</table>"
        "<h2>Artifacts</h2>"
        "<table><tr><th>Artifact</th><th>Name</th><th>Type</th><th>Size</th>"
        f"<th>SHA-256</th><th>Created</th></tr>{artifact_rows}</table>"
    )
    return _layout(f"Task {task['task_id']}", body, csrf=csrf)


def nodes_page(
    principals: list[dict[str, Any]],
    actors: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> str:
    principal_rows = "".join(
        f"<tr><td>{_esc(p['principal_id'])}</td><td>{_esc(p['kind'])}</td>"
        f"<td>{_esc(p['display_name'])}</td></tr>"
        for p in principals
    )
    actor_rows = "".join(
        f"<tr><td>{_esc(a['actor_id'])}</td><td>{_esc(a['principal_id'])}</td>"
        f"<td>{_esc(a['kind'])}</td><td>{_esc(', '.join(a['capabilities']))}</td></tr>"
        for a in actors
    )
    node_rows = "".join(
        f"<tr><td>{_esc(n['node_id'])}</td><td>{_esc(n['actor_id'])}</td>"
        f"<td>{_esc(n['display_name'])}</td>"
        f"<td><span class=\"pill\">{_esc(n['status'])}</span></td>"
        f"<td>{_fmt(n['last_seen_at'])}</td></tr>"
        for n in nodes
    )
    body = (
        "<h1>Nodes &amp; Identities</h1>"
        "<h2>Principals</h2>"
        "<table><tr><th>Principal</th><th>Kind</th><th>Name</th></tr>"
        f"{principal_rows}</table>"
        "<h2>Actors</h2>"
        "<table><tr><th>Actor</th><th>Principal</th><th>Kind</th>"
        f"<th>Capabilities</th></tr>{actor_rows}</table>"
        "<h2>Nodes</h2>"
        "<table><tr><th>Node</th><th>Actor</th><th>Name</th><th>Status</th>"
        f"<th>Last seen</th></tr>{node_rows}</table>"
    )
    return _layout("Nodes & Identities", body)


def runs_page(runs: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td>{_esc(run['run_id'])}</td><td>{_esc(run.get('task_id') or '-')}</td>"
        f"<td>{_esc(run['principal_id'])}</td><td>{_esc(run['node_id'])}</td>"
        f"<td><span class=\"pill\">{_esc(run['status'])}</span></td>"
        f"<td>{_fmt(run['started_at'])}</td>"
        f"<td>{_fmt(run.get('completed_at'))}</td></tr>"
        for run in runs
    )
    if not rows:
        rows = '<tr><td colspan="7" class="muted">No runs.</td></tr>'
    body = (
        "<h1>Runs</h1>"
        "<table><tr><th>Run</th><th>Task</th><th>Principal</th><th>Node</th>"
        f"<th>Status</th><th>Started</th><th>Completed</th></tr>{rows}</table>"
    )
    return _layout("Runs", body)


def artifacts_page(artifacts: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td>{_esc(a['artifact_id'])}</td><td>{_esc(a['name'])}</td>"
        f"<td>{_esc(a.get('task_id') or '-')}</td><td>{_esc(a.get('run_id') or '-')}</td>"
        f"<td>{_esc(a['media_type'])}</td><td>{_esc(a['size_bytes'] or '-')}</td>"
        f"<td>{_fmt(a['created_at'])}</td></tr>"
        for a in artifacts
    )
    if not rows:
        rows = '<tr><td colspan="7" class="muted">No artifacts.</td></tr>'
    body = (
        "<h1>Artifacts</h1>"
        "<table><tr><th>Artifact</th><th>Name</th><th>Task</th><th>Run</th>"
        f"<th>Type</th><th>Size</th><th>Created</th></tr>{rows}</table>"
    )
    return _layout("Artifacts", body)


def tenants_page(
    tenants: list[dict[str, Any]],
    *,
    csrf: str,
    error: str | None = None,
) -> str:
    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""
    rows = "".join(
        f"<tr><td><a href=\"/web/tenants/{_esc(t['tenant_id'])}\">"
        f"{_esc(t['tenant_id'])}</a></td>"
        f"<td>{_esc(t['display_name'])}</td>"
        f"<td>{_fmt(t['created_at'])}</td></tr>"
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
        f"<table><tr><th>Tenant</th><th>Name</th><th>Created</th></tr>{rows}</table>"
    )
    return _layout("Tenants", body, csrf=csrf)


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
        f"<tr><td>{_esc(t['token_id'])}</td><td>{_esc(t['role'])}</td>"
        f"<td>{_esc(t['label'])}</td><td>{_esc(t.get('actor_id') or '-')}</td>"
        f"<td>{_esc(t.get('node_id') or '-')}</td>"
        f"<td>{_fmt(t['created_at'])}</td>"
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
        "<table><tr><th>Token</th><th>Role</th><th>Label</th><th>Actor</th>"
        f"<th>Node</th><th>Created</th><th>Status</th><th>Action</th></tr>"
        f"{token_rows}</table>"
    )
    return _layout(f"Tenant {tenant['tenant_id']}", body, csrf=csrf)


def not_found_page() -> str:
    return _layout("Not found", "<h1>404</h1><p>Page not found.</p>")
