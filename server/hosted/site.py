"""The public face of AskHooman (askhooman.com): a small landing page at / and the icon kit next to it (favicons, touch
icon, web manifest, link-preview image) from server/hosted/static/, copied from design/icon-kit/03_Website_PWA.

Files are read once at startup; a missing one fails the deploy, not a request. No scripts anywhere.
"""

from html import escape
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .pages import page

STATIC = Path(__file__).parent / "static"
TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
         ".webmanifest": "application/manifest+json"}
FILES = ("favicon.ico", "favicon.svg", "favicon-32x32.png", "apple-touch-icon.png", "icon-192x192.png",
         "icon-512x512.png", "icon-maskable-192x192.png", "icon-maskable-512x512.png", "site.webmanifest",
         "og-image-1200x630.png", "brandmark.svg")
FILE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",  # an SVG never runs anything
    "X-Content-Type-Options": "nosniff",
}
TAGLINE = "Questions from your AI agents, waiting for your decision."


def _file(name: str):
    body = (STATIC / name).read_bytes()
    media_type = TYPES[Path(name).suffix]

    async def endpoint(request: Request) -> Response:
        return Response(body, media_type=media_type, headers=FILE_HEADERS)
    return endpoint


def landing_page(public_url: str):
    image = escape(f"{public_url}/og-image-1200x630.png")
    head = (f'<meta name="description" content="{TAGLINE}"><meta property="og:type" content="website">'
            f'<meta property="og:title" content="AskHooman"><meta property="og:description" content="{TAGLINE}">'
            f'<meta property="og:url" content="{escape(public_url)}/"><meta property="og:image" content="{image}">'
            f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{image}">')
    body = (f'<img class="appicon" src="/icon-192x192.png" alt="" width="88" height="88">'
            f"<h1>AskHooman</h1><p>{TAGLINE}</p>"
            f'<p class="muted">You answer in the AskHooman app on iPhone or Mac. Agents connect over MCP at '
            f"<b>{escape(public_url)}/mcp</b>: add one in the app’s Settings.</p>")

    async def endpoint(request: Request) -> Response:
        return page("Questions from your AI agents", body, head=head, index=True)
    return endpoint


def routes(public_url: str) -> list[Route]:
    return ([Route(f"/{name}", _file(name), methods=["GET", "HEAD"]) for name in FILES]
            + [Route("/", landing_page(public_url), methods=["GET", "HEAD"])])
