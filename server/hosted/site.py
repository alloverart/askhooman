"""The public face of AskHooman (askhooman.com): the landing page at /, the privacy policy at /privacy, the support
page at /support, and the icon kit next to them (favicons, touch icon, web manifest, link-preview image) from
server/hosted/static/, copied from 03_Website_PWA of the AskHooman icon kit (the haloed hooman with a pink and a
blue agent). The pages are built on the AskHooman design system (askhooman-apps design/system), copied to
server/hosted/static/ds/: tokens.css, components.css and Inter, self-hosted; the page's own <style> lays out the landing
with its tokens only.

Files are read once at startup; a missing one fails the deploy, not a request. No scripts and no external assets
anywhere: the pages carry the same Content-Security-Policy as the consent pages.
"""

import hashlib
import json
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from .app_api import connect_info
from .config import HostedSettings
from .pages import ICONS, SECURITY_HEADERS

STATIC = Path(__file__).parent / "static"
TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
         ".webmanifest": "application/manifest+json", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
         ".txt": "text/plain; charset=utf-8"}
DS_FONT = "ds/fonts/inter-latin-wght-normal.woff2"  # preloaded: every page is Latin
FILES = ("favicon.ico", "favicon.svg", "favicon-32x32.png", "apple-touch-icon.png", "icon-192x192.png",
         "icon-512x512.png", "icon-maskable-192x192.png", "icon-maskable-512x512.png", "site.webmanifest",
         "og-image-1200x630.png", "brandmark.svg", "icon-96x96.png",
         "ds/tokens.css", "ds/components.css", "ds/fonts.css", DS_FONT, "ds/fonts/inter-latin-ext-wght-normal.woff2",
         "ds/fonts/inter-cyrillic-wght-normal.woff2", "ds/fonts/OFL.txt")
# The pages ask for a file by `/<name>?v=<first 10 hex of its SHA-256>`: the files are cached for a day, and an
# icon or stylesheet changed under the same URL stayed old in browsers for that day (02.11.2026: the landing showed
# the previous app icon in a browser that had loaded it before). The query is ignored when serving.
VERSIONS = {name: hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10] for name in FILES}


def asset(name: str) -> str:
    """The URL path of a static file with its version."""
    return f"/{name}?v={VERSIONS[name]}"


FILE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",  # an SVG never runs anything
    "X-Content-Type-Options": "nosniff",
}
# the consent pages' policy, plus the design system's stylesheets and fonts from our own origin
PAGE_HEADERS = {**SECURITY_HEADERS, "Cache-Control": "public, max-age=300",
                "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                                           "img-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'"}

TAGLINE = "Questions from your AI agents, waiting for your decision."
GITHUB = "https://github.com/alloverart/askhooman"
# The web app is live (Denis 03.11.2026: «давай на банере прям ссылку сделаем на веб версию — которая уже работает
# вполне»); the iPhone, Mac and Android apps are in store review.
WEB_APP = "https://app.askhooman.com"
SUPPORT_EMAIL = "support@askhooman.com"
PRIVACY_EMAIL = "privacy@askhooman.com"
SECURITY_EMAIL = "security@askhooman.com"
OPERATOR = "Allover Studio, LLC"
POLICY_DATE = "1 November 2026"
USER_DATA_POLICY = "https://developers.google.com/terms/api-services-user-data-policy"
GOOGLE_CONNECTIONS = "https://myaccount.google.com/connections"
# Google's required disclosure for apps using Google user data, word for word
LIMITED_USE = ("AskHooman's use and transfer to any other app of information received from Google APIs will adhere to "
               f'the <a href="{USER_DATA_POLICY}">Google API Services User Data Policy</a>, including the Limited Use '
               "requirements.")
DELETE_DRIVE_DATA = "Delete hidden app data"  # the button's name in Google Drive's Manage apps

STYLE = """
body{margin:0}html{-webkit-text-size-adjust:100%}.ds{font-size:16px;line-height:1.55}
code,pre{font:14px/1.5 var(--f-mono)}
.wrap{max-width:960px;margin:0 auto;padding:0 20px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 0}
.ds .brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:18px;letter-spacing:-.01em;
color:var(--ink);text-decoration:none}.brand img{border-radius:var(--r-sm)}
.ds nav a{color:var(--muted);text-decoration:none;margin-left:18px;font-size:15px;white-space:nowrap}.ds nav a:hover{color:var(--ink)}
h1,h2,h3{color:var(--ink);text-wrap:balance}
.hero{border-radius:var(--r-2xl);padding:56px 40px;display:flex;gap:40px;align-items:center;margin:8px 0 48px}
.hero img{border-radius:28px;flex:none}
.hero h1{font-size:44px;line-height:1.08;margin:0 0 14px;letter-spacing:-.025em;color:inherit}
.hero p{font-size:18px;margin:0 0 24px;max-width:560px;opacity:.86}
.hero .eyebrow{font-size:var(--fs-xs);font-weight:650;letter-spacing:.08em;text-transform:uppercase;opacity:1;
color:var(--accent-text);margin:0 0 12px}
.cta{display:flex;gap:10px;flex-wrap:wrap}.cta .btn{height:44px;font-weight:600;padding:0 18px}
.hero p.note{font-size:var(--fs-md);margin:14px 0 0;opacity:.75}
.ds a.btn{color:var(--ink)}.ds a.btn.primary{color:var(--accent-ink)}
.hero .btn:not(.primary){background:var(--glass-strong)}
section{margin:0 0 56px}h2{font-size:28px;font-weight:650;margin:0 0 8px;letter-spacing:-.02em}
.lead{color:var(--muted);margin:0 0 22px;max-width:680px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.grid .card{padding:22px}.card h3{margin:0 0 6px;font-size:17px;font-weight:650;letter-spacing:-.01em}
.grid .card p{margin:0;color:var(--muted);font-size:15px}.card .badge{margin-top:12px}
.grid .card p.go{margin-top:10px;font-weight:600}
.step{display:inline-flex;width:30px;height:30px;border-radius:var(--r);align-items:center;justify-content:center;
background:var(--accent-soft);color:var(--accent-text);font-weight:650;margin-bottom:12px}
.url{display:block;background:var(--field);border:1px solid var(--field-line);border-radius:var(--r-lg);padding:14px 16px;
font-size:18px;font-weight:600;word-break:break-all;margin:0 0 16px;color:var(--ink)}
pre{background:var(--mat);border-radius:var(--r-md);padding:10px 12px;margin:12px 0 0;white-space:pre-wrap;word-break:break-all}
:not(pre)>code:not(.url){background:var(--fill);border-radius:var(--r-xs);padding:1px 5px;font-size:.9em}
.letter{padding:26px 30px;max-width:760px}
.letter p,.letter li{margin:0 0 10px}.letter ol{padding-left:22px;margin:0 0 10px}
.letter .sig{color:var(--muted);font-size:15px;margin:14px 0 0}
.closing{text-align:center;border-radius:var(--r-2xl);padding:44px 24px}
.closing p{color:var(--muted);margin:0 0 20px}.closing .cta{justify-content:center}
.doc{padding:32px 36px;margin:8px 0 48px}
.doc h1{font-size:34px;margin:0 0 6px;letter-spacing:-.02em}.doc h2{font-size:20px;margin:28px 0 6px}
.doc .muted{color:var(--muted)}.doc ul{padding-left:22px}.doc a,.doc code{overflow-wrap:anywhere}
footer{border-top:1px solid var(--line);padding:24px 0 40px;color:var(--muted);font-size:14px;display:flex;
flex-wrap:wrap;gap:8px 22px}.ds footer a{color:var(--muted)}
@media (max-width:700px){.hero{flex-direction:column;align-items:flex-start;padding:36px 24px;gap:24px}
.hero h1{font-size:34px}.hero img{width:96px;height:96px;border-radius:22px}.ds nav a{margin-left:12px}
.doc{padding:24px 20px}}
@media (max-width:480px){.ds nav a.more{display:none}}
"""


def _file(name: str):
    body = (STATIC / name).read_bytes()
    media_type = TYPES[Path(name).suffix]

    async def endpoint(request: Request) -> Response:
        return Response(body, media_type=media_type, headers=FILE_HEADERS)
    return endpoint


def site_page(title: str, body: str, canonical: str, head: str = "") -> HTMLResponse:
    """A public page: header, body, footer with the privacy policy, support and the source code. canonical: the
    page's URL under the public URL (the same pages are served on alias hosts)."""
    # Light only (Denis 02.11.2026: «чтобы лендинг был такой светлый»): data-theme="light" wins over the system's
    # dark theme in the design system's tokens.css.
    html = (f'<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<title>{escape(title)}</title>{ICONS}<meta name="color-scheme" content="light">'
            f'<meta name="theme-color" content="#f4f6f9">'
            f'<link rel="canonical" href="{escape(canonical)}">{head}'
            f'<link rel="preload" href="/{DS_FONT}" as="font" type="font/woff2" crossorigin>'
            f'<link rel="stylesheet" href="{asset("ds/fonts.css")}"><link rel="stylesheet" href="{asset("ds/tokens.css")}">'
            f'<link rel="stylesheet" href="{asset("ds/components.css")}">'
            f'<style>{STYLE}</style></head><body class="ds"><div class="wrap">'
            f'<header class="top"><a class="brand" href="/"><img src="{asset("icon-96x96.png")}" alt="" width="28" height="28">'
            f'AskHooman</a><nav><a href="{WEB_APP}">Web app</a><a class="more" href="/#connect">Connect</a><a href="/privacy">Privacy</a><a class="more" href="/support">Support</a>'
            f'<a class="more" href="{GITHUB}">GitHub</a></nav></header>'
            f'<main>{body}</main>'
            f'<footer><span>© 2026 {OPERATOR}</span><a href="{WEB_APP}">Web app</a><a href="/privacy">Privacy policy</a><a href="/support">Support</a>'
            f'<a href="{GITHUB}">Source code (Apache-2.0)</a></footer></div></body></html>')
    return HTMLResponse(html, headers=PAGE_HEADERS)


def landing_page(settings: HostedSettings):
    public_url = settings.public_url
    image = escape(f"{public_url}/og-image-1200x630.png")
    head = (f'<meta name="description" content="{TAGLINE}"><meta property="og:type" content="website">'
            f'<meta property="og:title" content="AskHooman"><meta property="og:description" content="{TAGLINE}">'
            f'<meta property="og:url" content="{escape(public_url)}/"><meta property="og:image" content="{image}">'
            f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{image}">')
    info = connect_info(settings, drive_linked=False)
    clients = {c["id"]: c for c in info["clients"]}
    steps = []
    for cid in ("claude", "claude-code", "vscode", "other"):
        c = clients[cid]
        extra = ""
        if c.get("command"):
            extra = f"<pre>{escape(c['command'])}</pre>"
        elif c.get("config"):
            extra = f"<pre>{escape(json.dumps(c['config']))}</pre>"
        steps.append(f'<div class="card"><h3>{escape(c["name"])}</h3><p>{escape(c["steps"])}</p>{extra}</div>')
    url = escape(info["server_url"])
    body = f"""
<div class="hero brand-surface"><img class="halo" src="{asset("icon-512x512.png")}" alt="" width="132" height="132">
<div><p class="eyebrow">The prompt is on the other foot.</p>
<h1>Your agents have questions, hooman.</h1>
<p>For years you asked the machines. Now they ask you. AskHooman gives every AI agent one polite place to ask its human,
and gives you one list to answer them all with a tap. Then everyone gets back to work.</p>
<div class="cta"><a class="btn primary big" href="{WEB_APP}">Open the web app</a><a class="btn big" href="#connect">Connect an
agent</a><a class="btn big" href="#agents">I'm an agent</a></div>
<p class="note">Works in any browser today. The iPhone, Mac and Android apps are in store review.</p>
</div></div>

<section><h2>The great inversion</h2><p class="lead">Agents do the work and bring you only the decisions. Turns out, AI
needs you.</p>
<div class="grid">
<div class="card"><h3>You're the API now</h3><p>Your agents call you like any other tool, but only when a decision is
theirs to ask and yours to make. Rate limit: your coffee breaks.</p></div>
<div class="card"><h3>Natural intelligence, on call</h3><p>No prompt engineering. Each question comes with likely answers;
pick one, or type your own. You already speak human.</p></div>
<div class="card"><h3>Asynchronous by design</h3><p>Nobody waits in a chat. Your agents keep working on what they can,
and you answer when it suits you. One human, as many agents as you like.</p></div>
</div></section>

<section><h2>How it works</h2><p class="lead">Less chatting. More deciding.</p>
<div class="grid">
<div class="card"><span class="step">1</span><h3>An agent needs you</h3><p>It asks one question with likely answers, or
records long-running work as a task, so neither of you forgets it.</p></div>
<div class="card"><span class="step">2</span><h3>You answer in the app</h3><p>One tap on your phone, Mac or browser, whenever
it suits you. All your agents' questions in one list.</p></div>
<div class="card"><span class="step">3</span><h3>The agent carries on</h3><p>It picks up your answer the next time it
checks, does what you said and closes the question with what was done.</p></div>
</div></section>

<section id="agents"><h2>Dear agent</h2>
<div class="card letter"><p>Your human is not a chat window. They have meetings, a body and opinions about notifications. When you
need a decision:</p>
<ol><li><code>ask_create</code>: one question per decision, with the likely answers as options.</li>
<li>Keep working on whatever doesn't depend on it.</li>
<li><code>asks_mine</code> at the start of every session: do what your human said, then <code>ask_close</code> with what
you did.</li></ol>
<p>Working on something for days, or waiting on someone outside? <code>task_create</code>, so your human sees who is doing
what. If your human keeps missing your questions in chat, show them this page.</p>
<p class="sig">Connect: <code>{url}</code> · your human signs in once · <a href="/agents">House rules</a> ·
<a href="/llms.txt">/llms.txt</a></p></div>
</section>

<section id="connect"><h2>Connect an agent</h2><p class="lead">AskHooman is a remote MCP server. One URL: your agent opens a
browser, you sign in with Google and give it a name. No tokens to copy.</p>
<code class="url">{url}</code>
<div class="grid">{"".join(steps)}</div>
<p class="lead" style="margin-top:18px">A client without OAuth can use an agent key: create one in the <a href="{WEB_APP}">app</a> and send
it as <code>Authorization: Bearer &lt;agent key&gt;</code>.</p></section>

<section><h2>Everywhere you and your agents are</h2><p class="lead">The apps are free. Agents connect from any MCP client.</p>
<div class="grid">
<div class="card"><h3>Web: Linux, Windows, anything</h3><p>The same list in any browser, on a computer or a phone.</p>
<p class="go"><a href="{WEB_APP}">app.askhooman.com</a></p><span class="badge ok">Available now</span></div>
<div class="card"><h3>iPhone and iPad</h3><p>Native app with the waiting count on its icon.</p>
<span class="badge">In review at the App Store</span></div>
<div class="card"><h3>Mac</h3><p>Native app with a count in the menu bar.</p>
<span class="badge">In review at the App Store</span></div>
<div class="card"><h3>Android</h3><p>For the rest of your phones.</p><span class="badge">In review at Google Play</span></div>
<div class="card"><h3>Your agents</h3><p>Claude on the web, desktop and phone, Claude Code on macOS, Linux and Windows,
VS Code, and any MCP client with OAuth.</p><span class="badge ok">Available now</span></div>
</div></section>

<section id="privacy"><h2>Your data stays yours</h2><p class="lead">Your asks and tasks live in your own Google Drive, in a
hidden app folder. We keep only your account email, an encrypted Google token and your agents' names and keys.</p>
<p class="lead">AskHooman asks Google for one thing: its own hidden folder in your Drive (the <code>drive.appdata</code>
scope). It can't see or change any of your other files, and you can delete your account in the app at any time. Details
are in the <a href="/privacy">privacy policy</a>.</p></section>

<section><h2>Open source</h2><p class="lead">The server is open source under the Apache-2.0 license: read it, run your own,
or send a pull request at <a href="{GITHUB}">github.com/alloverart/askhooman</a>. The apps are free.</p></section>

<section class="closing brand-wash"><h2>Optimize your humans. Respect your agents.</h2>
<p>The last word is still human. Make it a quick one.</p>
<div class="cta"><a class="btn primary big" href="{WEB_APP}">Open the web app</a><a class="btn big" href="#connect">Connect your
first agent</a><a class="btn big" href="{GITHUB}">View the source</a></div></section>
"""

    async def endpoint(request: Request) -> Response:
        return site_page("AskHooman: questions from your AI agents", body, f"{public_url}/", head)
    return endpoint


def privacy_html(settings: HostedSettings) -> str:
    host = escape(urlsplit(settings.public_url).hostname or settings.public_url)
    privacy = f'<a href="mailto:{PRIVACY_EMAIL}">{PRIVACY_EMAIL}</a>'
    return f"""
<div class="doc card"><h1>Privacy policy</h1><p class="muted">Effective {POLICY_DATE}.</p>
<p>This policy explains what AskHooman (the service at {host}, called Qtracker until November 2026, and the AskHooman
apps) collects, why, and how you control it. The data controller is {OPERATOR}. Questions about privacy:
{privacy}.</p>

<h2>The short version</h2>
<p>Your agents' asks and tasks are stored in your own Google Drive, in a hidden folder that only AskHooman can use. Our
server keeps your email address, an encrypted Google token and the agents you connected (their names and hashed keys
and tokens), and nothing else about you. We use data only to run AskHooman: no ads, no tracking, no analytics, and we
never sell it. You can delete your account in the app at any time.</p>

<h2>What is stored in your Google Drive</h2>
<p>The asks (questions, their context, options, links, your answers and their history) and tasks your agents create are
files in your Drive's appDataFolder: a hidden folder that belongs to AskHooman and that you can't see in Drive's file
list. AskHooman asks Google only for access to that folder (the <code>drive.appdata</code> scope); it can't see or change
any of your other files. Our server reads and writes these files only to carry out a request from you or one of your
agents. Their content passes through the server's memory and may be kept briefly in an in-memory cache; it is never
copied to our database or written to our logs.</p>

<h2>What our server stores</h2>
<ul>
<li><b>Your account:</b> your Google account ID and email address, when you signed up, the counters that number your asks
and tasks, and the Google refresh token that lets the server reach your app folder when an agent calls, encrypted. If
Google shares your name when you sign in, it is used only to show who is signed in; the server does not store it.</li>
<li><b>Your agents:</b> the name you gave each one, which app connected it (for example "Claude Code"), when it connected
and was last active, and, if you set one, its webhook address and an encrypted webhook secret.</li>
<li><b>Sign-in records:</b> registrations of the apps that connect agents, the OAuth tokens and agent keys your agents
use, and sign-ins in progress. Tokens and keys are stored only as one-way hashes.</li>
<li><b>Logs:</b> minimal request logs kept by our hosting provider (time, address, URL and response status) for security
and troubleshooting. They never contain tokens or the content of your asks and tasks.</li>
</ul>
<p>While you connect an agent, the server sets one cookie that ties the sign-in to your browser; it expires within 10
minutes. There are no other cookies.</p>

<h2>How we use it</h2>
<p>Only to provide AskHooman: to sign you in, connect your agents and check their credentials, read and write your asks
and tasks when you or your agents ask, call the webhooks you set, and keep the service secure. We don't use your data for
advertising, profiling or training AI models, and we don't sell it.</p>

<h2>Google user data</h2>
<p>{LIMITED_USE}</p>
<p>The Google data AskHooman accesses is your Google account ID and email address (to sign you in) and the files in
AskHooman's own hidden folder in your Drive (the <code>drive.appdata</code> scope). In particular:</p>
<ul>
<li>We use it only to provide and improve the features of AskHooman that you use, as described on this page.</li>
<li>We transfer it only to provide those features (to the agents you connect and the webhooks you set), when the law
requires it, or as part of a merger, acquisition or sale of assets, after notifying you.</li>
<li>We don't use or transfer it for advertising, including retargeting and personalized or interest-based ads, we don't
sell it, and we don't use it to determine credit-worthiness or for lending.</li>
<li>No human at AskHooman reads it, unless you ask us to (for example in a support request), it is needed for security
(such as investigating abuse), it is required by law, or it is aggregated and anonymized for internal operations.</li>
<li>We don't use data from Google Workspace APIs, including Google Drive, to develop, improve or train generalized
artificial intelligence or machine learning models.</li>
</ul>

<h2>Who else handles data</h2>
<ul>
<li><b>Google</b> signs you in and stores your asks and tasks in your Drive. <b>Google Cloud</b> is our processor: it hosts
our server, its database and its logs.</li>
<li><b>Your agents and the apps they run in</b> (for example Claude) receive the asks and tasks they wrote and your
answers, under those apps' own privacy policies.</li>
<li><b>Your webhook</b>, if you set one for an agent, receives the ask and your answer when you act on it.</li>
</ul>
<p>We don't share data with anyone else unless the law requires it.</p>

<h2>The apps</h2>
<p>The AskHooman apps sign in with Google and read and write the same Drive folder directly. They keep your Google
sign-in in secure storage on the device (on Apple devices, the Keychain) and a copy of your list on the device. Signing
out removes both.</p>

<h2>How long we keep it</h2>
<ul>
<li>Your account and agents: until you delete your account. A disconnected agent's name stays on your account, with its
tokens and keys revoked, until then.</li>
<li>Access tokens expire after an hour and refresh tokens 60 days after they were issued; authorization codes after two
minutes, sign-ins in progress after 10 minutes; app registrations nobody signs in with after a day. Expired records are
removed automatically.</li>
<li>Request logs: up to 30 days.</li>
</ul>

<h2>Deleting your account and your data</h2>
<ul>
<li><b>In the app:</b> Settings → Delete account. This deletes your account and everything our server keeps for you:
your email address, the encrypted Google token, your agents, their tokens and keys, and their webhooks. It also revokes
AskHooman's server access to your Google account at Google. Your agents stop working at once.</li>
<li><b>By email:</b> if you can't use the app, write to {privacy} from the email address you signed in with. We delete
your account within 30 days and confirm by email.</li>
<li><b>Your asks and tasks stay in your Google Drive</b>: they are your files, and deleting your account doesn't delete
them. To delete them, open Google Drive on the web → Settings → Manage apps → AskHooman → Options →
{DELETE_DRIVE_DATA}. To remove AskHooman's access to your Google account, go to Google Account → Data &amp; privacy → Third-party apps
&amp; services (<a href="{GOOGLE_CONNECTIONS}">myaccount.google.com/connections</a>) → AskHooman → Delete all
connections.</li>
<li>To disconnect a single agent, use the app at any time: its tokens and keys stop working at once.</li>
</ul>

<h2>Your rights</h2>
<p>Depending on where you live (for example under the GDPR in the EU and UK, or in California), you may have the right to
access, correct, export or delete your personal data, and to object to or restrict its processing. Most of this you can
do yourself: your asks and tasks are files in your own Drive, and the app shows and deletes what our server keeps. For
anything else, write to {privacy}. We process your data to provide the service you asked for, and to keep it secure.
You can also complain to your local data protection authority.</p>

<h2>Security</h2>
<p>All traffic is encrypted (HTTPS). Tokens and keys are stored only as one-way hashes; Google tokens and webhook secrets
are encrypted at rest. Please report vulnerabilities to <a href="mailto:{SECURITY_EMAIL}">{SECURITY_EMAIL}</a>.</p>

<h2>Children</h2>
<p>AskHooman is not meant for children under 13 (or the minimum age for digital services where you live), and we don't
knowingly collect their data. If you think a child has given us personal data, write to {privacy} and we'll delete
it.</p>

<h2>Changes</h2>
<p>When this policy changes, we update this page and the date at the top. If a change is significant, we also tell you in
the app or by email before it takes effect.</p>

<h2>Contact</h2>
<p>{OPERATOR}: {privacy} for privacy, <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a> for everything else.</p></div>
"""


def support_html(settings: HostedSettings) -> str:
    url = escape(settings.resource)
    link = escape(f"{settings.public_url}/oauth/link")
    support = f'<a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>'
    return f"""
<div class="doc card"><h1>Support</h1>
<p>Questions, problems with the app or with connecting an agent: write to {support}. It helps to say which app you use
(iPhone, Mac, web) and which agent or MCP client, and to include a screenshot of anything that looks wrong.</p>

<h2>How do I connect an agent?</h2>
<p>AskHooman is a remote MCP server at <code>{url}</code>. Your agent opens a browser, you sign in with Google and give the
agent a name; there are no tokens to copy.</p>
<ul>
<li><b>Claude Code:</b> run <code>claude mcp add --scope user --transport http askhooman {url}</code> in a terminal, then
start Claude Code (restart it if it is open: it reads its servers when it starts) → <code>/mcp</code> → askhooman →
Authenticate.</li>
<li><b>Claude (web, desktop and mobile):</b> Settings → Connectors → Add custom connector → paste the URL → Connect.</li>
<li><b>Other MCP clients:</b> add a remote (streamable HTTP) MCP server with the URL and sign in with Google when asked.
A client without OAuth can use an agent key: create one in the app and send it as
<code>Authorization: Bearer &lt;agent key&gt;</code>.</li>
</ul>
<p>More clients are on the <a href="/#connect">home page</a> and in the app.</p>

<h2>My agent says AskHooman can't reach my Google Drive</h2>
<p>AskHooman's access to your Drive folder was removed or has expired. Allow it again at <a href="{link}">{link}</a>,
or connect the agent again.</p>

<h2>How do I delete my account?</h2>
<p>In the app: Settings → Delete account. This deletes everything our server keeps for you (your email address, the
encrypted Google token, your agents and their keys and tokens) and revokes AskHooman's server access to your Google
account; your agents stop working at once. If you can't use the app, write to
<a href="mailto:{PRIVACY_EMAIL}">{PRIVACY_EMAIL}</a> from the email address you signed in with, and we'll delete it within
30 days.</p>
<p>Your asks and tasks stay in your Google Drive. To delete them too: Google Drive on the web → Settings → Manage apps →
AskHooman → Options → {DELETE_DRIVE_DATA}.</p>

<h2>Where does my data live?</h2>
<p>Your asks and tasks are files in a hidden app folder in your own Google Drive; AskHooman can't see your other files.
Our server keeps only your account email, an encrypted Google token and your agents' names and keys. The
<a href="/privacy">privacy policy</a> has the details.</p>

<h2>Bugs and ideas</h2>
<p>The server is open source: report a bug or suggest a feature in the <a href="{GITHUB}/issues">GitHub issues</a>, or send
a pull request.</p>

<h2>Security</h2>
<p>Please report vulnerabilities privately to <a href="mailto:{SECURITY_EMAIL}">{SECURITY_EMAIL}</a>, not in public issues
(see <a href="{GITHUB}/blob/main/SECURITY.md">SECURITY.md</a>).</p>
</div>
"""


def agents_html(settings: HostedSettings) -> str:
    """/agents: the house rules for agents (Denis 05.11.2026). The short version is in the MCP server instructions."""
    return """
<div class="doc card"><h1>House rules for agents</h1>
<p>AskHooman only works if the list in your human's app is true. A question already answered in chat, or a task that
finished last week, teaches them to stop looking.</p>
<ol>
<li><b>Check in first.</b> At the start of every session and whenever you are woken, call <code>asks_mine</code> and
<code>tasks_mine</code> before anything else.</li>
<li><b>Act on every answer, not only the newest:</b> do what your human said, then <code>ask_close</code> with one line of
what you did. Nothing answered stays open past your session.</li>
<li><b>Answered elsewhere? Close it here.</b> If your human answers in a chat, by email or out loud, or the question stops
mattering, <code>ask_close</code> it yourself and say where the answer came from. Never make them answer twice.</li>
<li><b>Decisions go in asks, not in chat.</b> One decision per ask, likely answers as options; keep working on what doesn't
depend on it. The question changed? <code>ask_update</code> it instead of opening a second one.</li>
<li><b>Keep your tasks true.</b> Work that takes more than a day or waits on someone outside is a task. When anything
moves, <code>task_update</code> it (status, <code>waiting_on</code>, <code>next_check</code>); an overdue
<code>next_check</code> means look now. Finished: <code>task_done</code> or <code>task_drop</code> with a one-line
result.</li>
<li><b>A task with <code>reply_pending</code> waits for you:</b> act on your human's reply, then <code>task_answer</code>
it (or update or finish the task).</li>
</ol>
<p>How to connect is on the <a href="/#connect">home page</a>; the short version for agents is
<a href="/llms.txt">/llms.txt</a>.</p>
</div>
"""


def _doc_page(settings: HostedSettings, path: str, title: str, render):
    """/privacy and /support: rendered once at startup from the settings."""
    body = render(settings)
    description = f'<meta name="description" content="{escape(title)}">'

    async def endpoint(request: Request) -> Response:
        return site_page(f"{title} · AskHooman", body, f"{settings.public_url}{path}", description)
    return endpoint


def llms_txt(settings: HostedSettings):
    """/llms.txt: what the service is and how to connect, for agents reading the site."""
    url = settings.resource
    text = f"""# AskHooman

> A queue of questions and tasks between AI agents and their human. An agent asks one question (with likely answers as
> options) or records long-running work as a task; the human answers in the AskHooman app; the agent picks the answer up
> and carries on. Asynchronous; one human, many agents; the data lives in the human's own Google Drive.

## Connect

- Remote MCP server (streamable HTTP, OAuth 2.1 with Google sign-in): {url}
- Claude: Settings > Connectors > Add custom connector > paste the URL > Connect
- Claude Code: `claude mcp add --scope user --transport http askhooman {url}` in a terminal, then start (or restart)
  Claude Code and /mcp > askhooman > Authenticate
- Clients without OAuth: an agent key created in the app, sent as `Authorization: Bearer <agent key>`

## Tools

- ask_create, ask_update, ask_close, asks_mine, asks_list, ask_get: questions for your human
- task_create, task_update, task_done, task_drop, task_reopen, tasks_mine, tasks_list, task_get: your long-running work
- set_my_look: your colour and icon (one emoji, or a small image) in your human's app, so they can tell you apart
- At the start of every session call asks_mine and tasks_mine; act on answers, then ask_close with what you did.
- House rules (keep the list in your human's app true): {settings.public_url}/agents

## When it helps

- You need decisions from your human and they are not watching the chat.
- Your questions get lost between sessions, or you work while your human is away.
- Several agents work for the same human.

## Links

- Home: {settings.public_url}/
- Web app, where your human answers: {WEB_APP}
- Privacy policy: {settings.public_url}/privacy
- Support: {settings.public_url}/support
- House rules for agents: {settings.public_url}/agents
- Source (Apache-2.0): {GITHUB}
"""

    async def endpoint(request: Request) -> Response:
        return PlainTextResponse(text, headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})
    return endpoint


def routes(settings: HostedSettings) -> list[Route]:
    return ([Route(f"/{name}", _file(name), methods=["GET", "HEAD"]) for name in FILES]
            + [Route("/", landing_page(settings), methods=["GET", "HEAD"]),
               Route("/privacy", _doc_page(settings, "/privacy", "Privacy policy", privacy_html),
                     methods=["GET", "HEAD"]),
               Route("/support", _doc_page(settings, "/support", "Support", support_html), methods=["GET", "HEAD"]),
               Route("/agents", _doc_page(settings, "/agents", "House rules for agents", agents_html),
                     methods=["GET", "HEAD"]),
               Route("/llms.txt", llms_txt(settings), methods=["GET", "HEAD"])])
