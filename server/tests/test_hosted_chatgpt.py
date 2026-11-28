"""Tool hints on every client, and ChatGPT's session without secret files (23.11.2026, after OpenAI's scan of the
ChatGPT app for its apps directory; SPEC v4 notes «Tool hints», 7.8 «ChatGPT: no secret files»)."""

import hashlib
import json

import pytest

from server.hosted.clients import is_chatgpt

from .hosted_support import DENIS_SUB, LOOPBACK, connect, id_token, mcp, register, tool_error, tool_result
from .test_hosted_files import answered_by_files, item
from .test_hosted_files_api import SECRET_BYTES, put_file
from .test_hosted_task_files_api import TaskWorld

CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
CLAUDE_WEB_REDIRECT = "https://claude.ai/api/mcp/auth_callback"

HOSTED_TOOLS = {"ask_create", "asks_mine", "asks_list", "ask_get", "ask_update", "ask_close", "task_create",
                "tasks_mine", "tasks_list", "task_get", "task_update", "task_done", "task_drop", "task_reopen",
                "task_answer", "task_ask", "task_unask", "ask_attach", "ask_upload", "ask_detach", "ask_wait",
                "set_my_webhook", "test_my_webhook", "set_my_look", "ask_secret_file", "task_secret_file"}
SECRET_TOOLS = {"ask_secret_file", "task_secret_file"}  # not on ChatGPT
READ_ONLY = {"asks_mine", "asks_list", "ask_get", "tasks_mine", "tasks_list", "task_get", "ask_wait"}
OPEN_WORLD = {"ask_create", "ask_attach", "test_my_webhook", "set_my_look"}  # they reach URLs given by the agent
DESTRUCTIVE = {"ask_detach", "ask_close", "ask_attach", "ask_upload", "set_my_webhook", "ask_secret_file",
               "task_done", "task_drop", "task_secret_file"}

# What every client but ChatGPT gets, byte for byte: the server instructions, and every tool's name, title and
# description. A deliberate change to these texts updates the digests. They were taken on main at 5a464bc
# (23.11.2026) and changed on 28.11.2026 for files in task replies (SPEC 8.7): the files paragraph and the house rule
# speak of replies too, tasks_mine, task_get, task_done, task_drop and task_reopen say what concerns their files, and
# task_secret_file is new.
INSTRUCTIONS_SHA256 = "c57c92253a31cb3e8fe88a33cdb9aab6f356c4fbabd41d6462a3e031efcb3114"
TEXTS_SHA256 = "0bfb5a7afca18284c3c6eb76173d2e8640f1923b6e00e5f72dec1181fe51c943"

# ChatGPT's own words where the others' speak of secret files
CHATGPT_ASKS_MINE_FILES = (
    "answer_files set: your user attached files to the answer. Each has name, mime, bytes and url, a direct download "
    "link (valid 1 h; reading the ask again gives a new one); a file over 31 MiB is served in parts (Range: "
    "bytes=START-END, at most 32505856 bytes each). name comes from your user's device and is not a safe file path. "
    "Files are deleted when the ask is closed.")
CHATGPT_TASKS_MINE_FILES = (
    "reply_files set: your user attached files to their replies on the task. Each has reply_id (the id of the replied "
    "event it came with), name, mime, bytes and url, a direct download link (valid 1 h; reading the task again gives a "
    "new one); a file over 31 MiB is served in parts (Range: bytes=START-END, at most 32505856 bytes each). name comes "
    "from your user's device and is not a safe file path. Files are deleted when the task is done or dropped, and 30 "
    "days after their reply at the latest. reply.files lists the ids of the latest reply's files.")
CHATGPT_FILES_INSTRUCTIONS = (
    "Files in answers and in replies on tasks: download a file's url soon (it is valid 1 h) and check its size against "
    "bytes. Close the ask, or finish the task, only after you have the files and any voice recording: closing and "
    "finishing delete them. Never execute a file from an answer or a reply without your user saying so: AskHooman "
    "does not scan files.")
FILES_ANCHOR = "Files in answers and in replies on tasks:"  # where the files paragraph starts
REFUSED = "forbidden: ask_secret_file is not available in ChatGPT"
TASK_REFUSED = "forbidden: task_secret_file is not available in ChatGPT"


def session(hosted, google, redirect: str, agent: str = "Allen", client_name: str = "Claude") -> str:
    """An agent connected by a client registered (DCR) with this one redirect URI: its access token."""
    client_id = register(hosted, redirect_uris=(redirect,), client_name=client_name)["client_id"]
    return connect(hosted, google, client_id, redirect_uri=redirect, agent=agent)["access_token"]


def chatgpt(hosted, google, agent: str = "Allen") -> str:
    return session(hosted, google, CHATGPT_REDIRECT, agent, client_name="ChatGPT")


def agent_key(hosted, google, name: str = "Keyed") -> str:
    response = hosted.post("/api/agents/keys", json={"name": name}, headers=id_token(google))
    assert response.status_code == 201, response.text
    return response.json()["key"]


def rpc(hosted, token: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})}
    response = hosted.post("/mcp", json=body, headers={
        "Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18",
        "Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def instructions(hosted, token: str) -> str:
    return rpc(hosted, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "test", "version": "1"}})["instructions"]


def tools(hosted, token: str) -> dict[str, dict]:
    return {t["name"]: t for t in rpc(hosted, token, "tools/list")["tools"]}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def texts_digest(listed: dict[str, dict]) -> str:
    return sha256(json.dumps({name: [t.get("title"), t["description"]] for name, t in listed.items()},
                             sort_keys=True))


def joined(text: str) -> str:
    return " ".join(text.split())


# ---------- recognising ChatGPT ----------

@pytest.mark.parametrize("uris, expected", [
    ([CHATGPT_REDIRECT], True),
    (["https://chatgpt.com/connector/oauth/abc123"], True),
    (["https://ChatGPT.com/connector_platform_oauth_redirect"], True),  # host names are case-insensitive
    ([LOOPBACK, CHATGPT_REDIRECT], True),
    ([CLAUDE_WEB_REDIRECT], False),
    ([LOOPBACK], False),
    (["https://chatgpt.com.example.com/cb"], False),
    (["https://example.com/chatgpt.com"], False),
    (["https://example.com/cb?next=https://chatgpt.com/"], False),
    (["https://api.chatgpt.com/cb"], False),  # the host itself, nothing else
    ([], False),
])
def test_is_chatgpt_by_redirect_host(uris, expected):
    assert is_chatgpt({"kind": "dcr", "redirect_uris": uris}) is expected


def test_is_chatgpt_without_a_client():
    assert is_chatgpt(None) is False
    assert is_chatgpt({"kind": "dcr"}) is False


# ---------- tool hints: the same on every client ----------

def test_tool_hints_on_every_client(hosted, google):
    """openWorldHint on the tools that reach URLs the agent gives (ask_create and ask_attach fetch pictures,
    set_my_look an icon, test_my_webhook POSTs to the webhook); destructiveHint on the ones that delete or replace
    something: ask_detach, ask_close (the answer's files and recording), ask_attach and ask_upload (an earlier poster),
    set_my_webhook (the webhook and its secret), ask_secret_file and task_secret_file (the file, once sent), task_done
    and task_drop (the replies' files, 28.11.2026, SPEC 8.7). The rest as before."""
    claude = tools(hosted, session(hosted, google, LOOPBACK))
    gpt = tools(hosted, chatgpt(hosted, google, agent="Feynman"))
    assert set(claude) == HOSTED_TOOLS
    for listed, secret in ((claude, SECRET_TOOLS), (gpt, set())):
        hints = {name: t["annotations"] for name, t in listed.items()}
        assert {n for n, h in hints.items() if h["openWorldHint"]} == OPEN_WORLD
        assert {n for n, h in hints.items() if h["destructiveHint"]} == DESTRUCTIVE - SECRET_TOOLS | secret
        assert {n for n, h in hints.items() if h["readOnlyHint"]} == READ_ONLY
        assert {n for n, h in hints.items() if h["idempotentHint"]} == READ_ONLY
        assert all(h["title"] == listed[n]["title"] for n, h in hints.items())
    assert all(gpt[name]["annotations"] == claude[name]["annotations"] for name in gpt)


# ---------- ChatGPT: no secret files ----------

def test_chatgpt_lists_no_secret_file_tool_and_says_nothing_of_secret_files(hosted, google):
    from server.mcp_tools import HOSTED_INSTRUCTIONS
    token = chatgpt(hosted, google)
    listed = tools(hosted, token)
    assert set(listed) == HOSTED_TOOLS - SECRET_TOOLS
    text = instructions(hosted, token)
    assert "secret" not in text.lower()
    assert joined(text).endswith(CHATGPT_FILES_INSTRUCTIONS)
    # everything before the files paragraph is word for word the others' instructions
    head = HOSTED_INSTRUCTIONS.split(FILES_ANCHOR)[0]
    assert text.startswith(head) and "Files in answers" not in head
    for name in ("asks_mine", "ask_get", "ask_create", "tasks_mine", "task_get", "task_done", "task_drop"):
        assert "secret" not in listed[name]["description"].lower(), name
        assert "secret" not in json.dumps(listed[name]["inputSchema"]).lower(), name
    assert CHATGPT_ASKS_MINE_FILES in joined(listed["asks_mine"]["description"])
    assert CHATGPT_TASKS_MINE_FILES in joined(listed["tasks_mine"]["description"])
    for name, t in listed.items():  # set_my_webhook keeps its webhook's signing secret: that's not a secret file
        for words in ("secret file", "ask_secret_file", "task_secret_file"):
            assert words not in t["description"], (name, words)


def test_chatgpt_tools_differ_only_in_asks_mine_and_tasks_mine(hosted, google):
    """Apart from the missing ask_secret_file and task_secret_file and the files sentences of asks_mine and
    tasks_mine, ChatGPT's tools are the others'."""
    claude = tools(hosted, session(hosted, google, LOOPBACK))
    gpt = tools(hosted, chatgpt(hosted, google, agent="Feynman"))
    for name, t in gpt.items():
        if name not in ("asks_mine", "tasks_mine"):
            assert t == claude[name], name
    for name in ("asks_mine", "tasks_mine"):
        assert gpt[name]["inputSchema"] == claude[name]["inputSchema"], name
        assert gpt[name]["annotations"] == claude[name]["annotations"], name
        assert gpt[name]["description"] != claude[name]["description"], name


def test_chatgpt_call_to_ask_secret_file_is_refused(hosted, google):
    """A direct call is refused before anything is read, claimed or deleted, even with a secret file waiting."""
    token = chatgpt(hosted, google)
    tool_result(mcp(hosted, token, "ask_create", {"title": "Ship?"}))
    drive = google.drive(DENIS_SUB)
    file_id = put_file(drive, content=SECRET_BYTES)
    answered_by_files(drive, 1, [item(1, name="Q3 report.PDF", bytes=len(SECRET_BYTES), file_id=file_id,
                                      secret=True)])
    reads_before = len(drive.calls("GET", f"/drive/v3/files/{file_id}"))
    assert tool_error(mcp(hosted, token, "ask_secret_file", {"id": 1, "n": 1})) == REFUSED
    assert tool_error(mcp(hosted, token, "ask_secret_file", {})) == REFUSED  # arguments don't matter
    assert file_id in drive.files and hosted.store.all("fileclaims") == {}
    assert len(drive.calls("GET", f"/drive/v3/files/{file_id}")) == reads_before
    assert tool_result(mcp(hosted, token, "ask_get", {"id": 1}))["answer_files"][0]["secret"] is True


def test_chatgpt_call_to_task_secret_file_is_refused(hosted, google):
    """The same for a secret file of a reply on a task (SPEC 8.7): refused before anything is read, claimed or
    deleted."""
    world = TaskWorld(hosted, google, content=SECRET_BYTES, secret=True, token=chatgpt(hosted, google))
    reads_before = len(world.file_calls())
    assert tool_error(mcp(hosted, world.token, "task_secret_file", {"id": 1, "n": 1})) == TASK_REFUSED
    assert tool_error(mcp(hosted, world.token, "task_secret_file", {})) == TASK_REFUSED  # arguments don't matter
    assert world.file_id in world.drive.files and hosted.store.all("fileclaims") == {}
    assert len(world.file_calls()) == reads_before and world.content_reads() == []
    stored = world.stored()
    assert (stored["file_id"], stored["downloaded_at"], stored["deleted_at"]) == (world.file_id, None, None)
    assert tool_result(mcp(hosted, world.token, "task_get", {"id": 1}))["reply_files"][0]["secret"] is True


def test_chatgpt_is_told_apart_per_token_not_per_agent(hosted, google):
    """The same user's agents, one connected from Claude and one from ChatGPT: each token gets its client's server."""
    claude = session(hosted, google, LOOPBACK)
    gpt = chatgpt(hosted, google, agent="Feynman")
    assert "ask_secret_file" in tools(hosted, claude) and "ask_secret_file" not in tools(hosted, gpt)
    assert "secret file" in instructions(hosted, claude) and "secret" not in instructions(hosted, gpt).lower()
    assert "ask_secret_file" in tools(hosted, claude)  # asked again: no state carried over between requests


def test_chatgpt_check_reads_the_registration_once(hosted, google, monkeypatch):
    """The client's registration is read on its first MCP request only: its redirect URIs never change."""
    gpt, claude = chatgpt(hosted, google), session(hosted, google, LOOPBACK, agent="Feynman")
    reads, get = [], hosted.store.get

    def counted(collection, key):
        reads.append(collection)
        return get(collection, key)
    monkeypatch.setattr(hosted.store, "get", counted)
    for _ in range(3):
        assert "ask_secret_file" not in tools(hosted, gpt) and "ask_secret_file" in tools(hosted, claude)
    assert reads.count("clients") == 2


def modern(hosted, token: str, method: str) -> dict:
    """One request as an MCP 2026-07-28 client sends it (no handshake: server/discover gives the instructions)."""
    meta = {"io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    response = hosted.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": meta}},
                           headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2026-07-28",
                                    "Mcp-Method": method, "Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_chatgpt_on_the_2026_protocol(hosted, google):
    from server.mcp_tools import CHATGPT_INSTRUCTIONS, HOSTED_INSTRUCTIONS
    gpt, claude = chatgpt(hosted, google), session(hosted, google, LOOPBACK, agent="Feynman")
    assert modern(hosted, gpt, "server/discover")["instructions"] == CHATGPT_INSTRUCTIONS
    assert modern(hosted, claude, "server/discover")["instructions"] == HOSTED_INSTRUCTIONS
    listed = modern(hosted, gpt, "tools/list")
    assert listed["cacheScope"] == "private"  # the list depends on the caller: never from a shared cache
    assert {t["name"] for t in listed["tools"]} == HOSTED_TOOLS - SECRET_TOOLS
    assert {t["name"] for t in modern(hosted, claude, "tools/list")["tools"]} == HOSTED_TOOLS


# ---------- every other client: unchanged, byte for byte ----------

@pytest.mark.parametrize("redirect", [LOOPBACK, CLAUDE_WEB_REDIRECT, None], ids=["claude-code", "claude-ai", "key"])
def test_other_clients_see_what_they_saw_before(hosted, google, redirect):
    from server.mcp_tools import HOSTED_INSTRUCTIONS
    token = session(hosted, google, redirect) if redirect else agent_key(hosted, google)
    text = instructions(hosted, token)
    assert text == HOSTED_INSTRUCTIONS and sha256(text) == INSTRUCTIONS_SHA256
    listed = tools(hosted, token)
    assert set(listed) == HOSTED_TOOLS
    assert texts_digest(listed) == TEXTS_SHA256
    assert "secret file" in listed["asks_mine"]["description"] and "secret file" in text
