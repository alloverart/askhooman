"""set_my_webhook / test_my_webhook (SPEC v5 "Waking the agent"): an agent that can receive an HTTPS POST (its own
server, n8n, Zapier) wires its webhook itself instead of asking its user to paste a URL into the app.

Only the calling agent's own webhook (the agent comes from the token, never from the arguments); the URL rules of
v4 (https, port 443, public host); the signing secret is returned only when it is new; 10 sets and 10 tests an hour
per agent, counted per call. The agent shape then says webhook_set_by: "agent"."""

import uuid

from ..db import utc_now
from ..rules import AskError, Invalid, NotFound
from .webhooks import check_webhook_url


class RateLimited(AskError):
    status_code = 429
    label = "rate limited"


def _own(accounts, who) -> dict:
    agent = accounts.own_agent(who.user, who.agent_id)
    if agent is None:
        raise NotFound("this agent was disconnected")
    return agent


def set_own_webhook(accounts, limiter, who, url, rotate_secret: bool = False) -> dict:
    """Set (or with url "" remove) the caller's webhook -> {webhook_url, webhook_set_by, webhook_secret?}."""
    if not limiter.allow("webhook_set", who.agent_id):
        raise RateLimited("too many webhook changes; at most 10 an hour")
    agent = _own(accounts, who)
    if not isinstance(url, str):
        raise Invalid("url must be an https URL, or \"\" to remove the webhook")
    url = url.strip()
    if url:
        try:
            check_webhook_url(url)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
    elif rotate_secret:
        raise Invalid("pass a url to have a webhook secret")
    agent, secret = accounts.set_webhook(agent, url or None, rotate=bool(rotate_secret), by="agent")
    reply = {"webhook_url": agent["webhook_url"], "webhook_set_by": agent["webhook_set_by"]}
    if secret:
        reply["webhook_secret"] = secret
    return reply


def ping_own_webhook(accounts, webhooks, limiter, who) -> dict:
    """test_my_webhook: one signed `ping` to the caller's webhook now, no retries -> {ok, status, error}; the
    outcome is also the agent's last_delivery."""
    if not limiter.allow("webhook_test", who.agent_id):
        raise RateLimited("too many webhook tests; at most 10 an hour")
    agent = _own(accounts, who)
    secret = accounts.webhook_secret(agent) if agent.get("webhook_url") else None
    if not secret:
        raise Invalid("you have no webhook: set one with set_my_webhook first")
    delivery = uuid.uuid4().hex
    payload = {"type": "ping", "agent": {"id": agent["id"], "name": agent["name"], "slug": agent["slug"]},
               "delivery_id": delivery, "sent_at": utc_now()}
    result = webhooks.try_once(agent["webhook_url"], secret, "ping", delivery, payload)
    accounts.record_delivery(agent["id"], result["ok"], result["status"], result["error"])
    return result
