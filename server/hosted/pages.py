"""Server-rendered pages of the hosted authorization server: consent, errors, "Drive linked".

Small, English, mobile-friendly, no external assets and no scripts. Every dynamic value is HTML-escaped.
Responses forbid framing (clickjacking on Allow), caching and referrers.
"""

from html import escape

from starlette.responses import HTMLResponse

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; manifest-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

STYLE = """
:root{color-scheme:light dark;--bg:#f6f6f4;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#d9d9de;--accent:#2f6fde;
--warn-bg:#fff5db;--warn-fg:#6b4e00}
@media (prefers-color-scheme:dark){:root{--bg:#111113;--card:#1c1c1f;--fg:#f2f2f4;--muted:#a1a1a6;--line:#3a3a3f;
--accent:#5b8ff0;--warn-bg:#3a2f12;--warn-fg:#f3d58a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:440px;margin:0 auto;padding:max(24px,env(safe-area-inset-top)) 16px 32px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}
h1{font-size:21px;margin:0 0 6px}p{margin:10px 0}.muted{color:var(--muted);font-size:14px}
.brand{display:flex;align-items:center;gap:8px;font-weight:600;letter-spacing:.2px;margin-bottom:14px;color:var(--muted)}
.brand img{border-radius:5px}.appicon{display:block;border-radius:20px;margin:2px 0 14px}
.warn{background:var(--warn-bg);color:var(--warn-fg);border-radius:10px;padding:10px 12px;font-size:14px}
label{display:block;font-weight:600;margin:18px 0 6px}
input[type=text]{width:100%;font:inherit;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
background:transparent;color:inherit}
.row{display:flex;flex-direction:row-reverse;gap:10px;margin-top:20px}button{flex:1;min-height:46px;font:inherit;font-weight:600;
border-radius:10px;border:1px solid var(--line);background:transparent;color:inherit;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
dl{margin:14px 0 0;font-size:14px}dt{color:var(--muted)}dd{margin:0 0 8px;word-break:break-all}
.error{color:#c62828;font-size:14px}
"""


ICONS = ('<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/svg+xml" href="/favicon.svg">'
         '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
         '<link rel="manifest" href="/site.webmanifest">')


def page(title: str, body: str, status: int = 200, head: str = "", index: bool = False) -> HTMLResponse:
    """head: extra trusted tags for <head> (the landing page's link preview); index: let search engines in."""
    robots = "" if index else '<meta name="robots" content="noindex">'
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'{robots}<title>{escape(title)} · Qtracker</title>{ICONS}{head}'
            f'<style>{STYLE}</style></head><body><main>'
            f'<div class="brand"><img src="/favicon.svg" alt="" width="20" height="20">Qtracker</div>'
            f'<div class="card">{body}</div></main></body></html>')
    return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)


def error_page(message: str, status: int = 400, title: str = "Can’t connect",
               back: tuple[str, str] | None = None) -> HTMLResponse:
    """back: (url, host) of a link that tells the app what happened; the user follows it (never automatic)."""
    tail = (f'<p><a href="{escape(back[0])}" rel="noreferrer">Return to {escape(back[1])}</a></p>' if back else
            '<p class="muted">Go back to the app you were connecting and start again.</p>')
    return page(title, f"<h1>{escape(title)}</h1><p>{escape(message)}</p>{tail}", status)


def consent_page(*, action_url: str, client_name: str, client_label: str, verified_by: str | None,
                 redirect_host: str, localhost: bool, email: str | None, agent_name: str, existing: list[str],
                 csrf: str, error: str | None = None, collision: dict | None = None) -> HTMLResponse:
    """Allow comes first in the form, so Enter in the name field allows (shown on the right).
    client_label: how the client is identified (its metadata URL host for CIMD clients); verified_by: None for
    dynamically registered clients, whose name nobody vouches for.
    collision: {name, slug, clients} of the user's existing agent the typed name belongs to, which this client
    never connected as: the page then asks for an explicit "Connect as existing agent" (or another name)."""
    who = f'<p class="muted">Signed in to Google as {escape(email)}.</p>' if email else ""
    source = (f"<dd>{escape(client_label)} (from its published metadata at {escape(verified_by)})</dd>"
              if verified_by else f"<dd>{escape(client_label)} (name given by the app itself, not verified)</dd>")
    warning = (f'<p class="warn">This app runs on your own computer: after you allow, your browser returns to '
               f"<b>{escape(redirect_host)}</b>. Only continue if you started this connection yourself.</p>"
               if localhost else "")
    hint = ""
    if existing:
        names = ", ".join(escape(n) for n in existing)
        hint = f'<p class="muted">Already connected: {names}.</p>'
    problem = f'<p class="error">{escape(error)}</p>' if error else ""
    if collision:
        via = ", ".join(escape(c) for c in collision["clients"])
        return page("Connect an agent", f"""
<h1>Connect as {escape(collision["name"])}?</h1>
<p class="warn">An agent named <b>{escape(collision["name"])}</b> is already connected (from {via}).
If you continue, <b>{escape(client_name)}</b> will act as {escape(collision["name"])}: it can read, change and close
{escape(collision["name"])}’s questions and tasks.</p>
{who}{warning}
<form method="post" action="{escape(action_url)}">
<input type="hidden" name="csrf" value="{escape(csrf)}">
<label for="agent_name">Connect as a new agent instead</label>
<input type="text" id="agent_name" name="agent_name" value="" maxlength="40" required
 autocomplete="off" autocapitalize="words" placeholder="My favorite agent">
<div class="row"><button type="submit" name="action" value="allow" class="primary">Allow as new agent</button>
<button type="submit" name="action" value="cancel">Cancel</button></div>
</form>
<form method="post" action="{escape(action_url)}">
<input type="hidden" name="csrf" value="{escape(csrf)}">
<input type="hidden" name="agent_name" value="{escape(collision["name"])}">
<input type="hidden" name="confirm" value="{escape(collision["slug"])}">
<div class="row"><button type="submit" name="action" value="allow">Connect as existing agent {escape(collision["name"])}
</button></div>
</form>
<dl><dt>App</dt>{source}<dt>Returns to</dt><dd>{escape(redirect_host)}</dd></dl>
""")
    body = f"""
<h1>Connect an agent</h1>
<p><b>{escape(client_name)}</b> wants to ask you questions and track its tasks in Qtracker.</p>
{who}{warning}
<form method="post" action="{escape(action_url)}">
<input type="hidden" name="csrf" value="{escape(csrf)}">
<label for="agent_name">Agent name</label>
<input type="text" id="agent_name" name="agent_name" value="{escape(agent_name)}" maxlength="40" required
 autocomplete="off" autocapitalize="words" placeholder="My favorite agent">
<p class="muted">Shown on its questions and tasks.</p>{hint}{problem}
<div class="row"><button type="submit" name="action" value="allow" class="primary">Allow</button>
<button type="submit" name="action" value="cancel">Cancel</button></div>
</form>
<dl><dt>App</dt>{source}<dt>Returns to</dt><dd>{escape(redirect_host)}</dd></dl>
"""
    return page("Connect an agent", body)


def linked_page(email: str | None) -> HTMLResponse:
    who = f" for {escape(email)}" if email else ""
    return page("Google Drive linked", f"<h1>Google Drive linked</h1><p>Qtracker can now keep your agents’ "
                                       f"questions and tasks in your Google Drive{who}.</p>"
                                       f'<p class="muted">You can close this window.</p>')
