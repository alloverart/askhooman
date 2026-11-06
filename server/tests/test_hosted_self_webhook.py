"""An agent that has a server (or n8n, Zapier) wires its own webhook (SPEC v5 "Waking the agent"):
set_my_webhook / test_my_webhook, only for itself, with the v4 rules for the URL and 10 sets and 10 tests an hour."""

import json

from server.hosted.webhooks import verify

from .hosted_support import connect, id_token, mcp, register, tool_error, tool_result


def allen(hosted, google) -> str:
    return connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]


def listed(hosted, google) -> dict:
    return hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]


def test_agent_sets_its_own_webhook(hosted, google):
    token = allen(hosted, google)
    first = tool_result(mcp(hosted, token, "set_my_webhook", {"url": "https://hooks.example/a"}))
    assert first["webhook_url"] == "https://hooks.example/a" and first["webhook_set_by"] == "agent"
    assert first["webhook_secret"].startswith("whsec_")
    assert listed(hosted, google)["webhook_set_by"] == "agent"
    again = tool_result(mcp(hosted, token, "set_my_webhook", {"url": "https://hooks.example/b"}))
    assert again["webhook_url"] == "https://hooks.example/b" and "webhook_secret" not in again  # shown once
    rotated = tool_result(mcp(hosted, token, "set_my_webhook", {"url": "https://hooks.example/b",
                                                                "rotate_secret": True}))
    assert rotated["webhook_secret"].startswith("whsec_") and rotated["webhook_secret"] != first["webhook_secret"]
    removed = tool_result(mcp(hosted, token, "set_my_webhook", {"url": ""}))
    assert removed == {"webhook_url": None, "webhook_set_by": None}
    assert listed(hosted, google)["webhook_url"] is None


def test_set_rules(hosted, google):
    token = allen(hosted, google)
    for bad in ("http://hooks.example/a", "https://hooks.example:8443/a", "https://10.0.0.1/a", "not a url"):
        assert tool_error(mcp(hosted, token, "set_my_webhook", {"url": bad})), bad
    assert "url" in tool_error(mcp(hosted, token, "set_my_webhook", {"url": "", "rotate_secret": True})).lower()
    for n in range(10 - 5):  # the five refusals above count too: the limit is on calls
        tool_result(mcp(hosted, token, "set_my_webhook", {"url": f"https://hooks.example/{n}"}))
    assert "too many" in tool_error(mcp(hosted, token, "set_my_webhook", {"url": "https://hooks.example/z"}))


def test_test_my_webhook(hosted, google):
    token = allen(hosted, google)
    assert "webhook" in tool_error(mcp(hosted, token, "test_my_webhook", {})).lower()
    secret = tool_result(mcp(hosted, token, "set_my_webhook", {"url": "https://hooks.example/t"}))["webhook_secret"]
    google.webhook_statuses.extend([200])
    assert tool_result(mcp(hosted, token, "test_my_webhook", {})) == {"ok": True, "status": 200, "error": None}
    call = google.webhook_calls[-1]
    assert verify(secret, call.content, call.headers["askhooman-signature"])
    body = json.loads(call.content)
    agent = listed(hosted, google)
    assert body["type"] == "ping" and body["agent"] == {"id": agent["id"], "name": "Allen", "slug": agent["slug"]}
    assert body["delivery_id"] == call.headers["askhooman-delivery"] and body["sent_at"]
    assert agent["last_delivery"]["ok"] is True and agent["last_delivery"]["status"] == 200
    google.webhook_statuses.extend([404])
    assert tool_result(mcp(hosted, token, "test_my_webhook", {})) == {"ok": False, "status": 404, "error": "HTTP 404"}
    assert listed(hosted, google)["last_delivery"]["error"] == "HTTP 404"
    for _ in range(10 - 3):
        tool_result(mcp(hosted, token, "test_my_webhook", {}))
    assert "too many" in tool_error(mcp(hosted, token, "test_my_webhook", {}))


def test_only_its_own_webhook(hosted, google):
    allen_token = allen(hosted, google)
    ogilvy = connect(hosted, google, register(hosted)["client_id"], agent="Ogilvy")["access_token"]
    tool_result(mcp(hosted, allen_token, "set_my_webhook", {"url": "https://hooks.example/allen"}))
    tool_result(mcp(hosted, ogilvy, "set_my_webhook", {"url": "https://hooks.example/ogilvy"}))
    hooks = {a["name"]: a["webhook_url"] for a in hosted.get("/api/agents", headers=id_token(google)).json()["agents"]}
    assert hooks == {"Allen": "https://hooks.example/allen", "Ogilvy": "https://hooks.example/ogilvy"}
