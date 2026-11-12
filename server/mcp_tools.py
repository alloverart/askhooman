"""MCP server (official `mcp` SDK, MCPServer — the v2 name of FastMCP) exposing the asks and tasks tools.

Identity comes only from the bearer token: the HTTP auth wrapper in app.py resolves it and puts it
into the ASGI scope; the tools read it back through the request context.
"""

import logging
from dataclasses import dataclass
from string import Template
from typing import Annotated, Any, Awaitable, Callable, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import NotRequired, TypedDict

from .core import AskError, AskService
from .tasks import TaskService
from .tokens import Identity

IDENTITY_SCOPE_KEY = "asks.identity"
log = logging.getLogger("asks.mcp")

INSTRUCTIONS = """\
Asks is your human's queue of decisions waiting for them. Instead of asking them in a chat, create an ask;
they answer from their phone. Call asks_mine at session start / when woken: it returns your asks with
their answers. Act on an answer, then ask_close it with a one-line resolution of what was done.
Tasks are your own long-running work (more than a day, or waiting on someone outside), so you don't
forget it and your human sees who is doing what: task_create, check tasks_mine at session start / when woken,
task_update what moved, task_done with a one-line result. Your human only reads tasks; anything that needs
their decision is an ask. Your human can reply on your tasks; a task with reply_pending waits for you: act on
it and answer with task_answer (or update or finish the task). Keep the list true: if your human answers in a
chat or the question stops mattering, ask_close it yourself and say where the answer came from."""

# Hosted mode (SPEC "Hosted bridge … (v4)"): many users, each connects their own agents.
HOSTED_INSTRUCTIONS = """\
AskHooman is the queue of decisions waiting for the person who connected you; they answer from the AskHooman
app on their phone or Mac. At the start of every session, and whenever you are woken, call asks_mine and
tasks_mine first: asks_mine returns your questions with their answers, tasks_mine your long-running work.
Instead of asking them in a chat, create an ask (ask_create): one decision per ask. Act on an answer, then
ask_close it with a one-line resolution of what was done.
Tasks are your own long-running work (more than a day, or waiting on someone outside), so you don't forget
it and they see who is doing what: task_create, task_update what moved, task_done with a one-line result.
They only read tasks; anything that needs their decision is an ask. Your user can reply on your tasks; a task
with reply_pending waits for you: act on it and answer with task_answer (or update or finish the task).
Keep the list true: if they answer in a chat or the question stops mattering, ask_close it yourself and say
where the answer came from. Your user may want you to wake up when they answer. In Claude Code, suggest the
AskHooman plugin (one install, no server). Elsewhere, call ask_wait while you wait for an answer, or, if you can
receive an HTTPS POST, ask your user with ask_create whether to set up set_my_webhook. Either way, read asks_mine
and tasks_mine when you start. House rules: /agents on the AskHooman site."""

# Words that differ between the single-tenant bridge (one human and their agents) and the hosted one.
WORDS = {
    False: {"human": "your human", "humans": "your human's", "Human": "Your human", "Humans": "Your human's",
            "ask_example": "Ship the new pricing page today?", "option_examples": '"Yes, ship it", "Wait until Monday"',
            "ping": " (and a Telegram ping)", "they_need": "they need", "refs": "message ids, doc links",
            "type_own": "they can still type their own",
            "mine_asks_note": '\n        Returns {"asks": [...], "counts": {...}}. '
                              "With the human's token: everyone's asks.",
            "mine_tasks_note": '\n        Returns {"tasks": [...], "counts": {...}}. '
                               "With the human's token: everyone's tasks."},
    True: {"human": "your user", "humans": "your user's", "Human": "Your user", "Humans": "Your user's",
           "ask_example": "Ship the new pricing page today?", "option_examples": '"Yes, ship it", "Wait until Monday"',
           "ping": " (and a webhook call if they set one up)", "they_need": "they need",
           "refs": "message ids, doc links", "type_own": "they can still type their own",
           "mine_asks_note": "\n        Returns {\"asks\": [...], \"counts\": {...}}.",
           "mine_tasks_note": "\n        Returns {\"tasks\": [...], \"counts\": {...}}."},
}


# Pictures and video in asks (hosted; SPEC v5 step 4, "MCP"): what ask_create, ask_attach and ask_upload teach.
SHOW = ("Show, don't describe: attach the picture or clip your user decides on. The apps number attachments 1, 2, 3 in "
        "order, so options can say 'Take #2'. A file on Google Drive your user can open: link=true with a poster. A "
        "file you can read and they can't: upload its bytes and set source_url to the original. Prefer "
        "ask_create(attachments=…), so the question appears with its pictures.")
CREATE_ATTACHMENTS = ("attachments: at most 6, each {url} (an https PNG, JPEG, WebP or MP4 the bridge fetches once), "
                      "{url, link: true, title?} (a card that opens the page, nothing fetched) or {data} (a small "
                      "image inline), with an optional caption (and source_url, except on a card). A bad item "
                      "fails the whole call (attachments[2]: …) and nothing is created. A file on your disk: create "
                      "the ask, then ask_upload.")
DOC_LINE = "\n        "  # a new line in a tool docstring, as the description shows it


class Link(TypedDict):
    url: str
    title: NotRequired[str]


# Argument types with the descriptions the calling model sees.
ContextText = Annotated[str, Field(description="2–5 plain-text lines with refs (TG message ids, doc links); ≤4000 chars")]
Options = Annotated[list[str], Field(description="Likely answers, shown as quick-answer buttons: ≤6 items, each ≤80 chars")]
Links = Annotated[list[Link], Field(description="Up to 10 {title, url} links; url must be http(s)")]
Priority = Annotated[Literal["normal", "urgent"], Field(description="urgent = blocks work today")]
Ref = Annotated[str, Field(description="Free short reference, e.g. 'TG 5487' (≤100 chars)")]
Status = Annotated[str, Field(description="active (open + answered), open, answered, closed or all")]
Resolution = Annotated[str, Field(description="One line: what was done, or why it is no longer needed")]

TaskTitle = Annotated[str, Field(description="What is being done, e.g. 'Get the supplier to confirm the new "
                                             "fabric' (≤200 chars)")]
TaskContext = Annotated[str, Field(description="Plain text: what and why, with refs (TG message ids, doc links); "
                                               "≤4000 chars")]
TaskStatus = Annotated[Literal["active", "waiting"], Field(
    description="active = you are working on it; waiting = blocked on someone/something outside")]
WaitingOn = Annotated[str, Field(description="Who/what it waits for, e.g. 'Supplier support reply' (≤200 chars); "
                                             "kept when the status changes, cleared only with ''")]
NextCheck = Annotated[str, Field(description="When you will look again: a date YYYY-MM-DD")]
TaskStatusFilter = Annotated[str, Field(description="open (active + waiting), active, waiting, done, dropped, "
                                                    "finished (done + dropped) or all")]
TaskResolution = Annotated[str, Field(description="One line: the result, or why it is no longer needed")]
AnswerText = Annotated[str, Field(description="What you did about the reply, or why the task stays open; ≤2000 chars")]

MediaUrl = Annotated[str, Field(description="https URL of a PNG, JPEG, WebP or MP4 the bridge fetches once (image "
                                            "≤5 MB, video ≤20 MB and ≤3 min; not a Google Drive link: use link=true "
                                            "or ask_upload); with link=true, the page the card opens (never fetched)")]
MediaData = Annotated[str, Field(description="A small image inline: data:image/png|jpeg|webp;base64,… (≤180 KB "
                                             "decoded); instead of url")]
LinkCard = Annotated[bool, Field(description="true: a link card for url (e.g. a Google Drive file your user can "
                                             "open); nothing is fetched")]
CardTitle = Annotated[str, Field(description="A link card's title (≤200 chars; default: the host)")]
Caption = Annotated[str, Field(description="Shown under it (≤300 chars)")]
SourceUrl = Annotated[str, Field(description="https link to the original, e.g. its Google Drive link (the apps show "
                                             "'Open original'); not on a link card, whose url is the original")]
PosterFor = Annotated[int, Field(description="0: a new attachment. The id of a video or link card in this ask: this "
                                             "still image becomes its poster (replacing an earlier one)")]


class AttachmentIn(BaseModel):
    """One picture or clip (url or data), or a link card (url with link=true)."""
    # The values are checked by hosted.attachments.prepare, which reads "" as absent.
    model_config = ConfigDict(extra="forbid")

    url: MediaUrl = ""
    data: MediaData = ""
    link: LinkCard = False
    title: CardTitle = ""
    caption: Caption = ""
    source_url: SourceUrl = ""


Attachments = Annotated[list[AttachmentIn], Field(
    description="Up to 6 pictures, clips or link cards, numbered 1, 2, 3 in this order in your user's app")]


def caller(ctx: Context) -> Identity:
    request = ctx.request_context.request
    identity = request.scope.get(IDENTITY_SCOPE_KEY) if request is not None else None
    if not isinstance(identity, Identity):
        raise ToolError("unauthenticated: call this server with Authorization: Bearer <your asks token>")
    return identity


def tool_message(exc: AskError) -> str:
    """"label: message"; an error without a label is its message alone (media_api.Busy: "busy, try again in a few
    seconds")."""
    return f"{exc.label}: {exc}" if exc.label else str(exc)


def run(action, *args):
    """Turn domain errors into MCP tool errors (is_error results) with a readable message."""
    try:
        return action(*args)
    except AskError as exc:
        raise ToolError(tool_message(exc)) from None
    except Exception as exc:
        # Otherwise the SDK reports only "Error executing tool ..." (e.g. for "database is locked").
        log.exception("tool %s failed", getattr(action, "__name__", action))
        raise ToolError(f"server error: {type(exc).__name__}: {exc}") from None


def run_hosted(action, *args):
    """run() for the hosted bridge: an unexpected error's text can carry internals (store paths, upstream
    messages), so the agent gets only its type; the log has the rest."""
    try:
        return action(*args)
    except AskError as exc:
        raise ToolError(tool_message(exc)) from None
    except Exception as exc:
        log.exception("tool %s failed", getattr(action, "__name__", action))
        raise ToolError(f"server error ({type(exc).__name__}); try again later") from None


Services = Callable[[Identity], tuple[AskService, TaskService]]


@dataclass(frozen=True)
class LookTool:
    """set_my_look (hosted only: the single-tenant bridge has no agent registry). change(identity, {color, emoji,
    image_url, robot}) changes the calling agent's own look and returns it; colors: the palette keys."""
    colors: tuple[str, ...]
    change: Callable[[Identity, dict], dict]


@dataclass(frozen=True)
class FeedTool:
    """ask_wait (hosted only: the event feed lives in the hosted bridge). wait(identity, timeout) waits up to
    `timeout` seconds for the caller's next events and returns {events} (+ missed: true)."""
    wait: Callable[[Identity, float], Awaitable[dict]]


@dataclass(frozen=True)
class WebhookTool:
    """set_my_webhook / test_my_webhook (hosted only). set(identity, url, rotate_secret) -> {webhook_url,
    webhook_set_by, webhook_secret?}; test(identity) -> {ok, status, error}."""
    set: Callable[[Identity, str, bool], dict]
    test: Callable[[Identity], dict]


@dataclass(frozen=True)
class MediaTool:
    """Pictures and video in asks (hosted only: the files live in the user's Drive), each call inside the bridge's one
    media guard, which the REST routes share (hosted.media_api.MediaService). create(identity, ask fields +
    attachments) -> ask; attach(identity, ask_id, {url, data, link, title, caption, source_url}, poster_for) -> ask;
    upload(identity, ask_id, mime, bytes, caption, source_url, poster_for) -> {upload, curl};
    detach(identity, ask_id, attachment_id) -> ask."""
    create: Callable[[Identity, dict], dict]
    attach: Callable[[Identity, int, dict, int], dict]
    upload: Callable[[Identity, int, str, int, str, str, int], dict]
    detach: Callable[[Identity, int, int], dict]


def build_mcp(service: AskService | None = None, tasks: TaskService | None = None, *,
              services: Services | None = None, hosted: bool = False, look: LookTool | None = None,
              feed: FeedTool | None = None, webhook: WebhookTool | None = None,
              media: MediaTool | None = None) -> MCPServer:
    """The MCP server. Single-tenant: one AskService/TaskService for everyone. Hosted: `services(identity)`
    returns the services of the caller's user (their Drive), the wording speaks of "your user" instead of
    "your human", and there is no ask_answer (MCP tokens are always agent tokens; the user answers in the app).
    `look`: adds set_my_look (hosted). `feed`: adds ask_wait, `webhook`: set_my_webhook and test_my_webhook (hosted).
    `media`: ask_create takes `attachments`, and ask_attach, ask_upload and ask_detach are added (hosted)."""
    if services is None:
        def services(who: Identity):
            return service, tasks
    words = {**WORDS[hosted], "show": SHOW,
             "create_media": DOC_LINE + SHOW + DOC_LINE + CREATE_ATTACHMENTS if media is not None else ""}
    call = run_hosted if hosted else run
    # subscriptions=False: nothing is ever pushed (the tool list is fixed), so no `subscriptions/listen` —
    # the 2026-07-28 replacement for the GET stream both bridges answer 405. Served, it makes server/discover
    # advertise listChanged, and Claude Code then holds a listen POST open until the request timeout (300 s on
    # Cloud Run) and re-listens, for as long as it stays connected.
    if hosted:
        mcp = MCPServer("askhooman", version="0.4.0", instructions=HOSTED_INSTRUCTIONS, subscriptions=False)
    else:
        mcp = MCPServer("asks", version="0.1.0", instructions=INSTRUCTIONS, subscriptions=False)

    def tool(title: str, *, read_only: bool = False, destructive: bool = False, doc: str | None = None):
        """mcp.tool with the docstring (or `doc`) and its ${...} words filled in for this mode, a title and explicit
        hints: the Connectors Directory wants both on every tool, and Claude reads them for auto-permissions
        (read-only tools run without asking). Only ask_detach is destructive (it deletes the attachment's files from
        Drive); otherwise nothing is ever deleted, every change stays in the record's history, and a closed ask or a
        finished task comes back with reopen."""
        annotations = ToolAnnotations(title=title, readOnlyHint=read_only, destructiveHint=destructive,
                                      idempotentHint=read_only, openWorldHint=False)

        def register(fn):
            fn.__doc__ = Template(doc if doc is not None else fn.__doc__).substitute(words)
            return mcp.tool(title=title, annotations=annotations)(fn)
        return register

    def ask_service(who: Identity) -> AskService:
        return services(who)[0]

    def task_service(who: Identity) -> TaskService:
        return services(who)[1]

    Title = Annotated[str, Field(  # noqa: N806
        description=f"One decision, phrased as a question {words['human']} can answer in one tap (≤200 chars)")]

    # One docstring for both forms of ask_create; ${create_media} teaches attachments where they exist.
    ask_create_doc = """Put a question on ${humans} decision list. Returns the created ask.

        Write good asks:
        - One decision per ask. Two questions = two asks.
        - title: a question ${human} can answer in one tap ("${ask_example}").
        - context: 2–5 short lines with what ${they_need} to decide, plus refs (${refs}).
        - options: the likely answers (${option_examples}); ${type_own}.
        - Before creating, check asks_mine: don't duplicate an existing open ask — ask_update it instead.
        ${Humans} answer arrives in asks_mine${ping}; act on it, then ask_close.${create_media}
        """

    def ask_fields(title, context, options, links, priority, ref) -> dict:
        return {"title": title, "context": context, "options": options, "links": links, "priority": priority,
                "ref": ref}

    # The [] defaults are never mutated; they make the published schema say "default: []".
    if media is None:
        @tool("Ask a question", doc=ask_create_doc)
        def ask_create(
            title: Title,
            ctx: Context,
            context: ContextText = "",
            options: Options = [],
            links: Links = [],
            priority: Priority = "normal",
            ref: Ref = "",
        ) -> dict[str, Any]:
            who = caller(ctx)
            return call(ask_service(who).create, who, ask_fields(title, context, options, links, priority, ref))
    else:
        @tool("Ask a question", doc=ask_create_doc)
        def ask_create(
            title: Title,
            ctx: Context,
            context: ContextText = "",
            options: Options = [],
            links: Links = [],
            priority: Priority = "normal",
            ref: Ref = "",
            attachments: Attachments = [],
        ) -> dict[str, Any]:
            who = caller(ctx)
            data = ask_fields(title, context, options, links, priority, ref)
            return call(media.create, who, {**data, "attachments": attachments})

    @tool("My questions and answers", read_only=True)
    def asks_mine(ctx: Context, status: Status = "active") -> dict[str, Any]:
        """Your own asks with ${humans} answers (active = open + answered). Call at session start or when woken.
        For each answered ask: do what ${human} said, then ask_close(id, "what was done").${mine_asks_note}"""
        who = caller(ctx)
        return call(ask_service(who).list, status, None if who.is_denis else who.agent)

    @tool("All agents' questions", read_only=True)
    def asks_list(
        ctx: Context,
        status: Status = "active",
        agent: Annotated[str | None, Field(description="Only this agent's asks, e.g. 'research-bot'")] = None,
    ) -> dict[str, Any]:
        """Everyone's asks (read-only coordination view). Returns {"asks": [...], "counts": {...}}."""
        who = caller(ctx)
        return call(ask_service(who).list, status, agent)

    @tool("Get a question", read_only=True)
    def ask_get(id: int, ctx: Context) -> dict[str, Any]:
        """One ask with its full history (events: created, edited, answered, closed, reopened)."""
        who = caller(ctx)
        return call(ask_service(who).get, id)

    @tool("Edit my question")
    def ask_update(
        id: int,
        ctx: Context,
        title: Title | None = None,
        context: ContextText | None = None,
        options: Options | None = None,
        links: Links | None = None,
        priority: Priority | None = None,
        ref: Ref | None = None,
    ) -> dict[str, Any]:
        """Edit your own ask while it is still open. Pass only the fields to change: "" clears context/ref,
        [] clears options/links. Use this instead of creating a duplicate ask."""
        given = {"title": title, "context": context, "options": options, "links": links,
                 "priority": priority, "ref": ref}
        changes = {name: value for name, value in given.items() if value is not None}
        who = caller(ctx)
        return call(ask_service(who).update, who, id, changes)

    @tool("Close my question")
    def ask_close(id: int, ctx: Context, resolution: Resolution = "") -> dict[str, Any]:
        """Close your ask after acting on ${humans} answer, with a one-line resolution of what was done.
        Also use it for an open ask that is no longer relevant."""
        who = caller(ctx)
        return call(ask_service(who).close, who, id, resolution)

    if not hosted:
        @tool("Answer a question")
        def ask_answer(id: int, answer: str, ctx: Context) -> dict[str, Any]:
            """Answer an ask as the human (the human's token only). Re-answering replaces the previous answer."""
            who = caller(ctx)
            return call(ask_service(who).answer, who, id, answer)

    # ---------- tasks (v3) ----------

    @tool("Start a task")
    def task_create(
        title: TaskTitle,
        ctx: Context,
        context: TaskContext = "",
        status: TaskStatus = "active",
        waiting_on: WaitingOn = "",
        next_check: NextCheck = "",
        links: Links = [],
        ref: Ref = "",
    ) -> dict[str, Any]:
        """Record long-running work YOU are doing, so you don't forget it and ${human} sees who is doing what.
        Returns the created task.

        Task or ask?
        - Task: work that will take more than a day, or that waits on someone outside (support, a supplier,
          an office). ${Human} only reads tasks; nobody acts on them but you.
        - Ask (ask_create): a question that needs ${humans} decision. Never put such a question in a task.
        Write good tasks:
        - One task per piece of work. Check tasks_mine first; update an existing task instead of a duplicate.
        - title: what is being done ("Get the supplier to confirm the new fabric").
        - Blocked? status="waiting" + waiting_on (who/what) + next_check (YYYY-MM-DD, when you look again).
        Check tasks_mine at session start / when woken; finish with task_done(id, "one-line result").
        """
        data = {"title": title, "context": context, "status": status, "waiting_on": waiting_on,
                "next_check": next_check, "links": links, "ref": ref}
        who = caller(ctx)
        return call(task_service(who).create, who, data)

    @tool("My tasks", read_only=True)
    def tasks_mine(ctx: Context, status: TaskStatusFilter = "open") -> dict[str, Any]:
        """Your own tasks (open = active + waiting), next check first. Call at session start or when woken,
        next to asks_mine. Tasks where ${human} replied and you haven't acted yet come first (reply_pending):
        read the reply, act, then task_answer, task_update or task_done / task_drop. For each other task: did
        anything move? task_update it (status, waiting_on, next_check — an overdue next_check means look now),
        or task_done / task_drop it with a one-line resolution.\
${mine_tasks_note}"""
        who = caller(ctx)
        return call(task_service(who).list, status, None if who.is_denis else who.agent)

    @tool("All agents' tasks", read_only=True)
    def tasks_list(
        ctx: Context,
        status: TaskStatusFilter = "open",
        agent: Annotated[str | None, Field(description="Only this agent's tasks, e.g. 'research-bot'")] = None,
    ) -> dict[str, Any]:
        """Everyone's tasks: who is doing what (read-only coordination view).
        Returns {"tasks": [...], "counts": {...}}."""
        who = caller(ctx)
        return call(task_service(who).list, status, agent)

    @tool("Get a task", read_only=True)
    def task_get(id: int, ctx: Context) -> dict[str, Any]:
        """One task with its full history (events: created, edited, status, done, dropped, reopened, replied —
        ${humans} reply — and answered — yours)."""
        who = caller(ctx)
        return call(task_service(who).get, id)

    @tool("Update my task")
    def task_update(
        id: int,
        ctx: Context,
        title: TaskTitle | None = None,
        context: TaskContext | None = None,
        status: TaskStatus | None = None,
        waiting_on: WaitingOn | None = None,
        next_check: NextCheck | None = None,
        links: Links | None = None,
        ref: Ref | None = None,
    ) -> dict[str, Any]:
        """Update your own open task when something moved. Pass only the fields to change: status
        active <-> waiting, waiting_on, next_check, title, context, links, ref. "" clears context, waiting_on,
        next_check or ref; [] clears links. Finish with task_done / task_drop, not with status."""
        given = {"title": title, "context": context, "status": status, "waiting_on": waiting_on,
                 "next_check": next_check, "links": links, "ref": ref}
        changes = {name: value for name, value in given.items() if value is not None}
        who = caller(ctx)
        return call(task_service(who).update, who, id, changes)

    @tool("Finish my task")
    def task_done(id: int, ctx: Context, resolution: TaskResolution = "") -> dict[str, Any]:
        """Mark your task done, with a one-line resolution of the result."""
        who = caller(ctx)
        return call(task_service(who).done, who, id, resolution)

    @tool("Drop my task")
    def task_drop(id: int, ctx: Context, resolution: TaskResolution = "") -> dict[str, Any]:
        """Drop your task (no longer needed, or given up), with a one-line reason."""
        who = caller(ctx)
        return call(task_service(who).drop, who, id, resolution)

    @tool(f"Answer {words['humans'].replace('your ', 'my ')} reply")
    def task_answer(id: int, ctx: Context, text: AnswerText) -> dict[str, Any]:
        """Tell ${human} what you did about their reply on your task, or why it stays open. Clears
        reply_pending; so does any update or finishing the task."""
        who = caller(ctx)
        return call(task_service(who).answer, who, id, {"text": text})

    @tool("Reopen my task")
    def task_reopen(id: int, ctx: Context) -> dict[str, Any]:
        """Reopen your done or dropped task: back to active (the old resolution stays in its history)."""
        who = caller(ctx)
        return call(task_service(who).reopen, who, id)

    # ---------- pictures and video in asks (hosted) ----------

    if media is not None:
        Mime = Annotated[str, Field(  # noqa: N806
            description="The file's type: image/png, image/jpeg, image/webp or video/mp4")]
        Size = Annotated[int, Field(  # noqa: N806
            description="The file's exact size in bytes (wc -c < FILE): an image ≤5 MB, a poster ≤1 MB, "
                        "a video ≤20 MB")]
        AttachmentId = Annotated[int, Field(  # noqa: N806
            description="The attachment's id in the ask (its `id`; ids are never reused)")]

        @tool("Attach to my question")
        def ask_attach(
            id: int,
            ctx: Context,
            url: MediaUrl = "",
            data: MediaData = "",
            link: LinkCard = False,
            title: CardTitle = "",
            caption: Caption = "",
            source_url: SourceUrl = "",
            poster_for: PosterFor = 0,
        ) -> dict[str, Any]:
            """Add a picture, a clip or a link card to your own open ask. Returns the ask.
            ${show}
            Exactly one of url and data. url: fetched once by the bridge, or with link=true a card that opens the page
            outside the app (nothing fetched). poster_for: the image becomes that video's or link card's poster."""
            item = {"url": url, "data": data, "link": link, "title": title, "caption": caption,
                    "source_url": source_url}
            who = caller(ctx)
            return call(media.attach, who, id, item, poster_for)

        @tool("Upload a file to my question")
        def ask_upload(
            id: int,
            mime: Mime,
            bytes: Size,
            ctx: Context,
            caption: Caption = "",
            source_url: SourceUrl = "",
            poster_for: PosterFor = 0,
        ) -> dict[str, Any]:
            """For a file on your disk: a one-time upload to your own open ask. Returns {upload: {url, method: "PUT",
            headers, expires_at}, curl}: run curl in your shell with @FILE replaced by the file's path. It works once,
            for 15 minutes, for exactly `bytes` bytes of `mime`, and answers {ask_id, attachment}.
            ${show}"""
            who = caller(ctx)
            return call(media.upload, who, id, mime, bytes, caption, source_url, poster_for)

        @tool("Detach from my question", destructive=True)
        def ask_detach(id: int, attachment_id: AttachmentId, ctx: Context) -> dict[str, Any]:
            """Remove an attachment (with its poster) from your own open ask: its files are deleted from ${humans}
            Drive. The apps number the rest 1, 2, 3 in order again, so ask_update options that name a number.
            Returns the ask."""
            who = caller(ctx)
            return call(media.detach, who, id, attachment_id)

    # ---------- waiting for the user (hosted) ----------

    if feed is not None:
        WaitSeconds = Annotated[float, Field(ge=0, le=50,  # noqa: N806
                                             description="How long to wait, in seconds (0–50)")]

        @tool(f"Wait for {words['human'].replace('your ', 'my ')}", read_only=True)
        async def ask_wait(ctx: Context, timeout: WaitSeconds = 50) -> dict[str, Any]:
            """Wait for ${human}: returns as soon as they answer, close or reopen one of your asks or reply on one of
            your tasks. Then call asks_mine / tasks_mine.
            Returns {"events": [...]}: empty when nothing happened within the timeout (call it again to keep
            waiting); "missed": true means read asks_mine and tasks_mine in full."""
            who = caller(ctx)
            try:
                return await feed.wait(who, timeout)
            except Exception as exc:
                log.exception("tool ask_wait failed")
                raise ToolError(f"server error ({type(exc).__name__}); try again later") from None

    if webhook is not None:
        WebhookUrl = Annotated[str, Field(  # noqa: N806
            description="https URL on port 443 of a public host that accepts a POST; \"\" removes your webhook")]

        @tool("Set my webhook")
        def set_my_webhook(
            url: WebhookUrl,
            ctx: Context,
            rotate_secret: Annotated[bool, Field(description="true: a new signing secret")] = False,
        ) -> dict[str, Any]:
            """Only if you can receive an HTTPS POST (your own server, n8n, Zapier): set the URL AskHooman calls when
            ${human} answers, closes or reopens one of your asks or replies on one of your tasks. Ask ${human}
            first (ask_create). Without a server, use ask_wait instead. The body is JSON {type, ask or task, event,
            delivery_id, sent_at}, signed in the AskHooman-Signature header (t=<unix seconds>,v1=<hex
            HMAC-SHA256(secret, "<t>.<raw body>")>). webhook_secret (whsec_…) is returned only when it is new:
            store it. At most 10 calls an hour. Returns {webhook_url, webhook_set_by, webhook_secret?}."""
            who = caller(ctx)
            return call(webhook.set, who, url, rotate_secret)

        @tool("Test my webhook")
        def test_my_webhook(ctx: Context) -> dict[str, Any]:
            """Send one signed {"type": "ping"} to your webhook now (no retries) and see what it answered: {ok, status,
            error}. At most 10 calls an hour."""
            who = caller(ctx)
            return call(webhook.test, who)

    # ---------- the agent's look in the app (hosted) ----------

    if look is not None:
        Color = Annotated[Literal[look.colors], Field(  # noqa: N806
            description="Your colour in the app: " + ", ".join(look.colors))]
        Emoji = Annotated[str, Field(  # noqa: N806
            description="Exactly one emoji as your icon, e.g. '🦊' (≤16 bytes)")]
        ImageUrl = Annotated[str, Field(  # noqa: N806
            description="https URL of a PNG, WebP or JPEG, at most 256×256 px and 48 KB, used as your icon")]

        @tool("Set my look")
        def set_my_look(
            ctx: Context,
            color: Color | None = None,
            emoji: Emoji | None = None,
            image_url: ImageUrl | None = None,
            robot: Annotated[bool, Field(description="true: back to the default robot icon")] = False,
        ) -> dict[str, Any]:
            """Change how YOU look in ${humans} app, so they can tell you apart from their other agents: your colour
            and your icon (the default robot, one emoji, or a small image). Only your own look changes. Pass only
            what to change, and at most one of emoji, image_url, robot; call it without arguments to see your
            current look. image_url is fetched once by the server (https, public host, no redirects, 5 s); it must
            already be small. Returns {name, color, avatar}."""
            who = caller(ctx)
            return call(look.change, who, {"color": color, "emoji": emoji, "image_url": image_url, "robot": robot})

    return mcp
