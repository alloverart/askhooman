"""MCP over real HTTP: the app runs under uvicorn, clients use the official SDK's streamable HTTP transport."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .conftest import ALLEN, CHAT, DENIS, HAMILTON, auth

TOOLS = {"ask_create", "asks_mine", "asks_list", "ask_get", "ask_update", "ask_close", "ask_answer",
         "task_create", "tasks_mine", "tasks_list", "task_get", "task_update", "task_done", "task_drop", "task_reopen",
         "task_answer"}


@asynccontextmanager
async def connect(base_url: str, token: str, mode: str = "legacy"):
    async with create_mcp_http_client(headers=auth(token)) as http:
        async with Client(streamable_http_client(f"{base_url}/mcp", http_client=http), mode=mode) as client:
            yield client


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def flatten(exc: BaseException):
    yield exc
    for sub in getattr(exc, "exceptions", ()):
        yield from flatten(sub)


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_mcp_round_trip(live_server, sender, notifier, mode):
    async def scenario():
        async with connect(live_server, ALLEN, mode) as allen:
            tools = await allen.list_tools()
            assert {t.name for t in tools.tools} == TOOLS
            create_doc = next(t for t in tools.tools if t.name == "ask_create").description
            assert "One decision per ask" in create_doc and "duplicate" in create_doc

            ask = payload(await allen.call_tool("ask_create", {
                "title": "Отдать ТЗ?",
                "context": "Раздел 7 плана",
                "options": ["Отдавай", "Сначала обсудим"],
                "links": [{"title": "План", "url": "https://docs.google.com/document/d/x"}],
                "ref": "TG 5486",
            }))
            assert (ask["agent"], ask["status"], ask["options"]) == ("allen", "open", ["Отдавай", "Сначала обсудим"])
            assert ask["links"] == [{"title": "План", "url": "https://docs.google.com/document/d/x"}]

            mine = payload(await allen.call_tool("asks_mine", {}))
            assert [a["id"] for a in mine["asks"]] == [ask["id"]]
            assert mine["counts"] == {"open": 1, "answered": 0, "closed": 0}

            updated = payload(await allen.call_tool("ask_update", {"id": ask["id"], "priority": "urgent", "ref": ""}))
            assert (updated["priority"], updated["ref"], updated["title"]) == ("urgent", None, "Отдать ТЗ?")

            # agents cannot answer, even their own ask
            assert "forbidden" in error_text(await allen.call_tool("ask_answer", {"id": ask["id"], "answer": "Да"}))

        async with connect(live_server, DENIS, mode) as denis:
            answered = payload(await denis.call_tool("ask_answer", {"id": ask["id"], "answer": "Отдавай"}))
            assert (answered["status"], answered["answer"]) == ("answered", "Отдавай")

        async with connect(live_server, ALLEN, mode) as allen:
            mine = payload(await allen.call_tool("asks_mine", {}))
            assert mine["asks"][0]["answer"] == "Отдавай"
            closed = payload(await allen.call_tool("ask_close", {"id": ask["id"], "resolution": "ТЗ в Асане"}))
            assert (closed["status"], closed["closed_by"], closed["resolution"]) == ("closed", "allen", "ТЗ в Асане")
            assert payload(await allen.call_tool("asks_mine", {}))["asks"] == []
            history = payload(await allen.call_tool("ask_get", {"id": ask["id"]}))
            assert [e["kind"] for e in history["events"]] == ["created", "edited", "answered", "closed"]
            everyone = payload(await allen.call_tool("asks_list", {"status": "all"}))
            assert [a["id"] for a in everyone["asks"]] == [ask["id"]]

    asyncio.run(scenario())
    assert notifier.flush()
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == CHAT
    assert sender.sent[0][1].startswith("Your ask #") and " was answered:\n" in sender.sent[0][1]


def test_mcp_identity_comes_from_token(live_server):
    async def scenario():
        async with connect(live_server, ALLEN) as allen:
            ask = payload(await allen.call_tool("ask_create", {"title": "Моё?"}))
        async with connect(live_server, HAMILTON) as hamilton:
            # an extra "agent" argument is not a parameter of ask_create; the token decides
            created = await hamilton.call_tool("ask_create", {"title": "Чьё?", "agent": "allen"})
            assert payload(created)["agent"] == "hamilton"
            assert "forbidden" in error_text(await hamilton.call_tool("ask_close", {"id": ask["id"]}))
            assert "forbidden" in error_text(await hamilton.call_tool("ask_update", {"id": ask["id"], "title": "x?"}))
            assert "forbidden" in error_text(await hamilton.call_tool("ask_answer", {"id": ask["id"], "answer": "x"}))
            mine = payload(await hamilton.call_tool("asks_mine", {"status": "all"}))
            assert all(a["agent"] == "hamilton" for a in mine["asks"])
            assert ask["id"] not in [a["id"] for a in mine["asks"]]

    asyncio.run(scenario())


def test_every_tool_has_a_title_and_explicit_hints(live_server):
    """The Connectors Directory checks every tool for a title and readOnlyHint / destructiveHint; Claude runs the
    read-only ones without asking. Nothing is destructive: every change stays in history and reopens."""
    async def scenario():
        async with connect(live_server, ALLEN) as allen:
            tools = (await allen.list_tools()).tools
        for t in tools:
            assert t.title and t.annotations and t.annotations.title == t.title, t.name
            assert t.annotations.read_only_hint is not None and t.annotations.destructive_hint is False, t.name
        assert {t.name for t in tools if t.annotations.read_only_hint} == {
            "asks_mine", "asks_list", "ask_get", "tasks_mine", "tasks_list", "task_get"}
        assert len(tools) == 16  # single-tenant: ask_answer too; v5: task_answer

    asyncio.run(scenario())


def test_single_tenant_has_no_attachment_tools(live_server):
    """Pictures in asks are hosted only (SPEC v5 step 4): the single-tenant bridge lists no attachment tools, its
    ask_create takes no `attachments` and its docstring doesn't teach them; its asks say `attachments: []`."""
    async def scenario():
        async with connect(live_server, ALLEN) as allen:
            tools = {t.name: t for t in (await allen.list_tools()).tools}
            assert set(tools) == TOOLS and not {"ask_attach", "ask_upload", "ask_detach"} & set(tools)
            create = tools["ask_create"]
            assert set(create.input_schema["properties"]) == {"title", "context", "options", "links", "priority", "ref"}
            assert "attachment" not in json.dumps(create.input_schema) and "Show, don't describe" not in \
                create.description
            assert create.description.rstrip().endswith("act on it, then ask_close.")
            assert payload(await allen.call_tool("ask_create", {"title": "Plain?"}))["attachments"] == []

    asyncio.run(scenario())


def test_mcp_errors_are_tool_errors(live_server):
    async def scenario():
        async with connect(live_server, ALLEN) as allen:
            assert "not found" in error_text(await allen.call_tool("ask_get", {"id": 999}))
            assert "not found" in error_text(await allen.call_tool("ask_get", {"id": 10**20}))
            assert "title" in error_text(await allen.call_tool("ask_create", {"title": "x" * 201}))
            assert "options" in error_text(await allen.call_tool("ask_create", {"title": "x?", "options": ["a"] * 7}))
            bad_link = await allen.call_tool("ask_create", {"title": "x?", "links": [{"url": "ftp://x"}]})
            assert "http(s)" in error_text(bad_link)
            assert error_text(await allen.call_tool("ask_create", {"title": "x?", "priority": "high"}))
            ask = payload(await allen.call_tool("ask_create", {"title": "x?"}))
            payload(await allen.call_tool("ask_close", {"id": ask["id"]}))
            assert "conflict" in error_text(await allen.call_tool("ask_close", {"id": ask["id"]}))
            assert "status" in error_text(await allen.call_tool("asks_mine", {"status": "pending"}))

    asyncio.run(scenario())


def test_mcp_rejects_bad_token(live_server):
    for headers in ({}, auth("wrong-token-0123456789abcdef")):
        response = httpx.post(
            f"{live_server}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
        assert response.status_code == 401
        assert response.json()["detail"] in ("missing bearer token", "invalid token")

    get = httpx.get(f"{live_server}/mcp", headers={"Accept": "text/event-stream"})
    assert get.status_code == 401

    async def scenario():
        async with connect(live_server, "wrong-token-0123456789abcdef") as client:
            await client.list_tools()

    with pytest.raises(BaseException) as raised:
        asyncio.run(asyncio.wait_for(scenario(), timeout=20))
    assert any(isinstance(e, MCPError) for e in flatten(raised.value)), repr(raised.value)


def test_mcp_plain_json_rpc_from_older_client_on_private_host(live_server):
    """What a 2025-era client (e.g. Claude Code registered with --header) sends; Host is a private address."""
    headers = {
        **auth(ALLEN),
        "Accept": "application/json, text/event-stream",
        "Host": "192.0.2.10:8790",
    }
    with httpx.Client(base_url=live_server, headers=headers, timeout=10) as http:
        init = http.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        })
        assert init.status_code == 200, init.text
        assert init.json()["result"]["serverInfo"]["name"] == "asks"
        version = init.json()["result"]["protocolVersion"]
        headers = {"MCP-Protocol-Version": version}
        assert http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                         headers=headers).status_code in (200, 202)
        call = http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "ask_create", "arguments": {"title": "Через сырой JSON-RPC?"}},
        })
        assert call.status_code == 200, call.text
        result = call.json()["result"]
        created = json.loads(result["content"][0]["text"])
        assert created["agent"] == "allen" and created["title"] == "Через сырой JSON-RPC?"

        # no standalone SSE stream on a stateless server
        get = http.get("/mcp", headers={"Accept": "text/event-stream", **headers})
        assert get.status_code == 405

    rest = httpx.get(f"{live_server}/api/asks", headers=auth(DENIS)).json()
    assert rest["counts"]["open"] == 1


def test_unexpected_errors_become_readable_tool_errors(app, live_server, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.state.service, "get", locked)

    async def scenario():
        async with connect(live_server, ALLEN) as allen:
            text = error_text(await allen.call_tool("ask_get", {"id": 1}))
            assert text.endswith("server error: OperationalError: database is locked")

    asyncio.run(scenario())


def test_mcp_task_round_trip(live_server, sender, notifier):
    async def scenario():
        async with connect(live_server, ALLEN) as allen:
            tools = {t.name: t for t in (await allen.list_tools()).tools}
            create_doc = tools["task_create"].description
            assert "more than a day" in create_doc and "ask_create" in create_doc and "decision" in create_doc
            assert "tasks_mine" in create_doc and "waiting_on" in create_doc
            assert "session start" in tools["tasks_mine"].description
            schema = tools["task_create"].input_schema["properties"]
            assert schema["status"]["enum"] == ["active", "waiting"] and schema["status"]["default"] == "active"
            assert tools["tasks_mine"].annotations.read_only_hint is True

            task = payload(await allen.call_tool("task_create", {
                "title": "Get the supplier to confirm the new fabric",
                "context": "Order form rejects size XS",
                "links": [{"url": "https://example.com/order"}],
                "ref": "TG 5591",
            }))
            assert (task["agent"], task["status"], task["waiting_on"], task["next_check"]) == ("allen", "active", None, None)
            assert task["links"] == [{"title": "https://example.com/order", "url": "https://example.com/order"}]
            waiting = payload(await allen.call_tool("task_update", {
                "id": task["id"], "status": "waiting", "waiting_on": "Supplier support reply",
                "next_check": "2026-11-03"}))
            assert (waiting["status"], waiting["waiting_on"], waiting["next_check"]) == (
                "waiting", "Supplier support reply", "2026-11-03")
            assert waiting["context"] == "Order form rejects size XS"  # None = unchanged
            mine = payload(await allen.call_tool("tasks_mine", {}))
            assert [t["id"] for t in mine["tasks"]] == [task["id"]]
            assert mine["counts"] == {"active": 0, "waiting": 1, "done": 0, "dropped": 0}
            # finishing goes through task_done, not status (the schema's enum rejects it)
            assert "active" in error_text(await allen.call_tool("task_update", {"id": task["id"], "status": "done"}))
            assert "next_check" in error_text(await allen.call_tool("task_update", {"id": task["id"],
                                                                                     "next_check": "Nov 3"}))
            assert "not found" in error_text(await allen.call_tool("task_get", {"id": 999}))

        async with connect(live_server, HAMILTON) as hamilton:
            for tool, args in [("task_update", {"title": "x"}), ("task_done", {}), ("task_drop", {}),
                               ("task_reopen", {})]:
                assert "forbidden" in error_text(await hamilton.call_tool(tool, {"id": task["id"], **args}))
            assert payload(await hamilton.call_tool("tasks_mine", {}))["tasks"] == []
            assert [t["id"] for t in payload(await hamilton.call_tool("tasks_list", {}))["tasks"]] == [task["id"]]

        async with connect(live_server, DENIS) as denis:
            assert "forbidden" in error_text(await denis.call_tool("task_create", {"title": "Мой?"}))
            assert "forbidden" in error_text(await denis.call_tool("task_done", {"id": task["id"]}))
            assert [t["id"] for t in payload(await denis.call_tool("tasks_mine", {}))["tasks"]] == [task["id"]]

        async with connect(live_server, ALLEN) as allen:
            done = payload(await allen.call_tool("task_done", {"id": task["id"], "resolution": "Supplier confirmed"}))
            assert (done["status"], done["resolution"]) == ("done", "Supplier confirmed") and done["finished_at"]
            assert "conflict" in error_text(await allen.call_tool("task_drop", {"id": task["id"]}))
            assert payload(await allen.call_tool("tasks_mine", {}))["tasks"] == []
            reopened = payload(await allen.call_tool("task_reopen", {"id": task["id"]}))
            assert (reopened["status"], reopened["resolution"], reopened["finished_at"]) == ("active", None, None)
            assert reopened["waiting_on"] == "Supplier support reply"  # kept until cleared explicitly
            dropped = payload(await allen.call_tool("task_drop", {"id": task["id"], "resolution": "moot"}))
            assert dropped["status"] == "dropped"
            history = payload(await allen.call_tool("task_get", {"id": task["id"]}))
            assert [e["kind"] for e in history["events"]] == ["created", "status", "done", "reopened", "dropped"]
            assert all(e["task_id"] == task["id"] and e["actor"] == "allen" for e in history["events"])
            finished = payload(await allen.call_tool("tasks_list", {"status": "finished"}))
            assert [t["id"] for t in finished["tasks"]] == [task["id"]]

    asyncio.run(scenario())
    assert notifier.flush() and sender.sent == []  # no Telegram for tasks


def test_mcp_task_answer_and_reply_pending_docs(live_server):
    """SPEC v5: the user replies on a task (REST, the app); the owner sees reply_pending and answers with
    task_answer."""
    from server.mcp_tools import HOSTED_INSTRUCTIONS, INSTRUCTIONS
    assert "task_answer" in INSTRUCTIONS and "task_answer" in HOSTED_INSTRUCTIONS

    async def scenario():
        async with connect(live_server, ALLEN) as allen, connect(live_server, HAMILTON) as hamilton:
            tools = {t.name: t for t in (await allen.list_tools()).tools}
            assert "reply_pending" in tools["tasks_mine"].description
            assert "replied" in tools["task_get"].description and "answered" in tools["task_get"].description
            assert tools["task_answer"].title == "Answer my human's reply"
            assert "what you did about their reply" in tools["task_answer"].description
            task = payload(await allen.call_tool("task_create", {"title": "Chase the supplier"}))
            async with httpx.AsyncClient(base_url=live_server) as http:
                r = await http.post(f"/api/tasks/{task['id']}/reply", json={"text": "Status?", "preset": "status"},
                                    headers=auth(DENIS))
                assert r.status_code == 200, r.text
            mine = payload(await allen.call_tool("tasks_mine", {}))
            assert mine["tasks"][0]["reply_pending"] is True and mine["tasks"][0]["reply"]["text"] == "Status?"
            assert "only the agent who owns" in error_text(
                await hamilton.call_tool("task_answer", {"id": task["id"], "text": "not mine"}))
            answered = payload(await allen.call_tool("task_answer", {"id": task["id"], "text": "Asked again today"}))
            assert answered["reply_pending"] is False
            payload(await allen.call_tool("task_done", {"id": task["id"]}))
            assert "done" in error_text(await allen.call_tool("task_answer", {"id": task["id"], "text": "late"}))

    asyncio.run(scenario())


def test_hosted_instructions_teach_waking(hosted, google, live_server):
    """SPEC v5 "Waking the agent": the hosted bridge tells agents how to be woken, and has the tools for it; the
    single-tenant bridge (no event feed, no agent registry) has none of them."""
    from server.mcp_tools import HOSTED_INSTRUCTIONS

    from .hosted_support import connect as hosted_connect
    from .hosted_support import register

    sentence = ("Your user may want you to wake up when they answer. In Claude Code, suggest the AskHooman plugin "
                "(one install, no server). Elsewhere, call ask_wait while you wait for an answer, or, if you can "
                "receive an HTTPS POST, ask your user with ask_create whether to set up set_my_webhook. Either way, "
                "read asks_mine and tasks_mine when you start.")
    assert sentence in " ".join(HOSTED_INSTRUCTIONS.split())
    waking = {"ask_wait", "set_my_webhook", "test_my_webhook"}
    token = hosted_connect(hosted, google, register(hosted)["client_id"], agent="Allen")["access_token"]
    listed = hosted.post("/mcp", headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18",
                                          "Accept": "application/json, text/event-stream"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    hosted_tools = {t["name"]: t for t in listed}
    assert waking <= set(hosted_tools)
    assert hosted_tools["ask_wait"]["annotations"]["readOnlyHint"] is True
    assert hosted_tools["ask_wait"]["description"].startswith(
        "Wait for your user: returns as soon as they answer, close or reopen one of your asks or reply on one of")
    single = httpx.post(f"{live_server}/mcp", headers={**auth(ALLEN), "MCP-Protocol-Version": "2025-06-18",
                                                       "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert not waking & {t["name"] for t in single}
    page = hosted.get("/agents").text
    assert "ask_wait" in page and "set_my_webhook" in page and "plugin" in page
    llms = hosted.get("/llms.txt").text
    assert "ask_wait" in llms and "set_my_webhook" in llms and "askhooman listen" in llms
