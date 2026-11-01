"""App API of the hosted bridge (SPEC v4 "App API"), for the AskHooman app's "Connect an agent" and
"Connected agents" screens. Authenticated with the user's Google ID token (audience = the iOS client).

  GET    /api/agents             the user's connected agents
  PATCH  /api/agents/{id}        {name?, webhook_url?, rotate_webhook_secret?} -> agent (+ webhook_secret, once)
  DELETE /api/agents/{id}        revoke: every token and key of the agent stops working at once
  POST   /api/agents/keys        {name} -> 201 {agent, key}: an agent key (qtk_…), shown only in this response
  GET    /api/connect-info       the MCP URL and per-client setup snippets
  POST   /api/events             {ask_id, kind: answered|closed|reopened} after the user acted in the app
                                 -> the asking agent's webhook is called (webhooks.py)
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..db import utc_now
from ..mcp_tools import IDENTITY_SCOPE_KEY
from ..rules import Conflict, Invalid, NotFound
from .accounts import Accounts, NameTaken, agent_json
from .config import HostedSettings
from .ratelimit import RateLimiter
from .records import DrivePool, HostedAsks
from .webhooks import WebhookSender, check_webhook_url

log = logging.getLogger("asks.hosted.app_api")

EVENT_STATUS = {"answered": "answered", "closed": "closed", "reopened": "open"}


@dataclass(frozen=True)
class AppUser:
    """The signed-in app user (from a verified Google ID token)."""
    sub: str
    email: str | None = None


def is_app_path(path: str) -> bool:
    return path == "/api/agents" or path.startswith("/api/agents/") or path in ("/api/connect-info", "/api/events")


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    webhook_url: str | None = None
    rotate_webhook_secret: bool = False


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    existing: bool = False  # add the key to the user's existing agent of that name (else 409 if there is one)


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ask_id: int
    kind: Literal["answered", "closed", "reopened"]


def connect_info(settings: HostedSettings, drive_linked: bool) -> dict:
    url = settings.resource
    return {
        "server_url": url,
        "drive_linked": drive_linked,
        "link_url": f"{settings.public_url}/oauth/link",
        "clients": [
            {"id": "claude", "name": "Claude (web, desktop and mobile)",
             "steps": "Settings → Connectors → Add custom connector → paste the URL → Connect"},
            {"id": "claude-code", "name": "Claude Code",
             "command": f"claude mcp add --transport http askhooman {url}",
             "steps": "Run the command, then /mcp in Claude Code → askhooman → Authenticate"},
            {"id": "vscode", "name": "VS Code",
             "config": {"servers": {"askhooman": {"type": "http", "url": url}}},
             "steps": "Add to .vscode/mcp.json (or the user mcp.json) and start the server"},
            {"id": "other", "name": "Any MCP client with OAuth",
             "steps": "Add a remote (streamable HTTP) MCP server with the URL; sign in with Google when asked"},
            {"id": "agent-key", "name": "Clients without OAuth (agent key)",
             "command": f'claude mcp add --transport http askhooman {url} --header "Authorization: Bearer <agent key>"',
             "steps": "Create an agent key in the app and send it as the header Authorization: Bearer <agent key>"},
        ],
    }


def add_app_api(app: FastAPI, settings: HostedSettings, accounts: Accounts, pool: DrivePool,
                webhooks: WebhookSender, limiter: RateLimiter) -> None:

    def app_user(request: Request) -> AppUser:
        user = request.scope.get(IDENTITY_SCOPE_KEY)  # set by RequestGuard from the ID token
        if not isinstance(user, AppUser):
            raise HTTPException(401, "sign in with Google", headers={"WWW-Authenticate": "Bearer"})
        if not limiter.allow("app", user.sub):
            raise HTTPException(429, "too many requests", headers={"Retry-After": "60"})
        return user

    def own(user: AppUser, agent_id: str) -> dict:
        agent = accounts.own_agent(user.sub, agent_id)
        if agent is None:
            raise NotFound(f"no agent {agent_id}")
        return agent

    @app.get("/api/agents")
    def list_agents(user: AppUser = Depends(app_user)):
        return {"agents": [agent_json(a) for a in accounts.agents(user.sub)]}

    @app.patch("/api/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentPatch, user: AppUser = Depends(app_user)):
        agent = own(user, agent_id)
        given = body.model_dump(exclude_unset=True)
        if body.name is not None:
            agent = accounts.rename_agent(agent, body.name)
        secret = None
        if "webhook_url" in given or body.rotate_webhook_secret:
            url = given.get("webhook_url", agent.get("webhook_url"))
            url = url.strip() if isinstance(url, str) else None
            if url:
                try:
                    check_webhook_url(url)
                except ValueError as exc:
                    raise Invalid(str(exc)) from None
            elif body.rotate_webhook_secret:
                raise Invalid("set a webhook_url to have a webhook secret")
            agent, secret = accounts.set_webhook(agent, url or None, rotate=body.rotate_webhook_secret)
        reply = {"agent": agent_json(agent)}
        if secret:
            reply["webhook_secret"] = secret  # shown once; store it in the receiver
        return reply

    @app.delete("/api/agents/{agent_id}")
    def revoke_agent(agent_id: str, user: AppUser = Depends(app_user)):
        return {"agent": agent_json(accounts.revoke_agent(own(user, agent_id)))}

    @app.post("/api/agents/keys", status_code=201)
    def create_key(body: KeyIn, user: AppUser = Depends(app_user)):
        try:
            agent, key = accounts.create_key(user.sub, body.name, existing=body.existing)
        except NameTaken as taken:
            raise Conflict(f"an agent named {taken.agent['name']} is already connected; send existing: true to "
                           "give it a key, or choose another name") from None
        return {"agent": agent_json(agent), "key": key, "drive_linked": accounts.drive_linked(user.sub),
                "link_url": f"{settings.public_url}/oauth/link"}

    @app.get("/api/connect-info")
    def get_connect_info(user: AppUser = Depends(app_user)):
        return connect_info(settings, accounts.drive_linked(user.sub))

    @app.post("/api/events", status_code=202)
    def post_event(body: EventIn = Body(...), user: AppUser = Depends(app_user)):
        ask = HostedAsks(pool.records(user.sub)).get(body.ask_id)
        if ask["status"] != EVENT_STATUS[body.kind]:
            raise Conflict(f"ask #{ask['id']} is {ask['status']} now, not {body.kind}")
        events = [e for e in ask.pop("events") if e.get("kind") == body.kind]
        event = {"id": events[-1].get("id"), "at": events[-1].get("at")} if events else None
        payload = {"type": f"ask.{body.kind}", "ask": ask, "event": event, "sent_at": utc_now()}
        if body.kind == "answered":
            payload["answer"] = ask["answer"]
        elif body.kind == "closed":
            payload["resolution"] = ask["resolution"]
        queued = 0
        for agent in accounts.agents(user.sub):
            if agent["slug"] != ask["agent"] or not agent.get("webhook_url"):
                continue
            secret = accounts.webhook_secret(agent)
            if not secret:
                continue
            delivery = uuid.uuid4().hex
            webhooks.send(agent["webhook_url"], secret, payload["type"], delivery, {**payload, "delivery_id": delivery})
            queued += 1
        return {"queued": queued}
