# Qtracker contract

Qtracker keeps a persistent list of **asks** (questions from AI agents waiting for one human's decision) and **tasks** (long-running work the agents do themselves and the human only reads). This file is the contract of the server: fields, statuses, REST, MCP, the Drive file format and the hosted OAuth server. Do not change field names without updating it.

Versions: v1 (single-tenant bridge with SQLite, REST, MCP, CLI), v2 (Drive mirror), v3 (tasks), v4 (hosted multi-tenant bridge). The human is identified by the role and actor id `denis` everywhere (tokens, `closed_by`, event `actor`, the default resolution `Not relevant (Denis)`): a legacy identifier that is part of the file format, read as "the human".

## Layout

```
SPEC.md                 this file (the contract)
README.md               overview, running, connecting agents
server/
  app.py                FastAPI app: `uvicorn server.app:app` (run from the repository root)
  hosted/               hosted mode (v4)
  requirements.txt
  tests/                pytest: REST, MCP round-trip, notify, Drive, hosted (all faked, no network)
cli/asks.py             stdlib-only CLI over REST
deploy/hosted/          Cloud Run image and guide
```

## Data model

Table `asks`:

| field | type | notes |
|---|---|---|
| id | int PK | autoincrement |
| agent | text | who asked: the agent's lowercase slug (e.g. `research-bot`) — taken from the caller's token, never from the body |
| title | text | required, 1–200 chars; one decision, phrased as a question the human can answer in one tap |
| context | text | optional, ≤ 4000 chars; plain text (NOT HTML/markdown); line breaks and bare URLs are meaningful |
| options | json array of strings | optional, ≤ 6 items, each 1–80 chars; quick-answer buttons |
| links | json array of `{title, url}` | optional, ≤ 10; url must be http(s) |
| priority | text | `normal` (default) or `urgent` |
| ref | text | optional, ≤ 100 chars, free reference (e.g. `TG 5487`) |
| status | text | `open` → `answered` → `closed` |
| answer | text | the human's answer (null until answered) |
| answered_at | text | ISO-8601 UTC with `Z` |
| resolution | text | why/how closed (optional) |
| closed_by | text | `denis` (the human) or the agent slug |
| closed_at | text | ISO-8601 UTC |
| created_at, updated_at | text | ISO-8601 UTC |

All timestamps carry milliseconds (`2026-10-31T06:21:17.391Z`) so ordering by them is exact. Empty `context`, `ref`, `resolution` are stored and returned as `null`. `resolution` ≤ 1000 chars. A link without a title gets its url as the title. Every text field (including answers, resolutions, link titles/urls) is stripped, `\r\n`/`\r` become `\n`, and control characters other than `\n` and `\t` are dropped (they would run as escape sequences in a terminal); text that is not valid Unicode (lone surrogates) → 422.

Table `ask_events`: `id, ask_id, at, actor, kind (created|edited|answered|closed|reopened), payload (json)` — full history, including previous answers.

## State machine

- `open`: waits for the human.
- `answer` (the human only): `open|answered` → `answered`. Re-answering while `answered` replaces the answer (old one stays in events) and notifies again; the same answer again is a no-op (no event, no notification).
- `close` (owner agent or the human): `open|answered` → `closed`. The human closing an open ask = "not relevant" (resolution defaults to `Not relevant (Denis)`).
- `reopen` (owner agent or the human): `closed|answered` → `open`; clears `answer`, `answered_at`, `resolution`, `closed_by`, `closed_at` (history kept in events).
- `edit` (owner agent only, only while `open`): title/context/options/links/priority/ref.
- Invalid transition → 409.

## Auth

`Authorization: Bearer <token>`. Tokens file (path in env `ASKS_TOKENS`), JSON:
`{"<token>": {"role": "denis"}, "<token2>": {"role": "agent", "agent": "research-bot"}, ...}` (role `denis` = the human; tokens ≥ 16 chars).
Re-read when the file's mtime changes (no restart needed to add an agent). Compare tokens in constant time. Missing/unknown token → 401. Wrong role / not owner → 403. Same tokens for REST and MCP.

## REST (JSON)

- `GET /healthz` (no auth) → `{"ok": true}`
- `GET /api/me` → `{"role": "denis"|"agent", "agent": "<slug>"|null}`
- `GET /api/asks?status=active|open|answered|closed|all&agent=<slug>&limit=<n>` → `{"asks": [...], "counts": {"open": n, "answered": n, "closed": n}}`
  - default `status=active` (= open + answered); `limit` default 200 (closed: most recent first)
  - order: `open` first — urgent before normal, then oldest first; then `answered` by `answered_at` desc; then `closed` by `closed_at` desc
  - `counts` respect the `agent` filter if given, otherwise they are global; they ignore `status` and `limit`
- `GET /api/asks/{id}` → ask object + `"events": [...]`
- `POST /api/asks` (agent role) body `{title, context?, options?, links?, priority?, ref?}` → 201 + ask
- `PATCH /api/asks/{id}` (owner agent, open only) body: any subset of the editable fields → ask. `null` means "leave unchanged" (same as MCP `ask_update`); clear `context`/`ref` with `""` and `options`/`links` with `[]`.
- `POST /api/asks/{id}/answer` (the human, role `denis`) `{answer}` (1–4000 chars) → ask
- `POST /api/asks/{id}/close` (owner or denis) `{resolution?}` → ask
- `POST /api/asks/{id}/reopen` (owner or denis) → ask
- Errors: `{"detail": "..."}` with 401/403/404/409/422 (413 for a body over 256 KB; an unexpected 500 keeps the same shape). `detail` is always one string (validation errors are joined). `/api/*` checks the token before reading the body. An id that cannot exist (≤ 0 or beyond 2^63−1) is a 404.
- No `/docs`, `/redoc`, `/openapi.json`: this file is the contract.
- Request bodies reject unknown fields with 422 — including `agent` on create (identity comes only from the token).

Ask object JSON: all fields from the table; `options` and `links` as arrays (never null — empty arrays).

## MCP

Streamable HTTP, mounted at `/mcp` on the same server, stateless, same bearer tokens (identity from the token — an agent can never act as another agent). Tools:

- `ask_create(title, context="", options=[], links=[], priority="normal", ref="")` → ask. Docstring must teach good asks: one decision per ask; title = question answerable in one tap; context 2–5 lines with refs (message ids, doc links); options = likely answers; don't duplicate an existing open ask — update it instead.
- `asks_mine(status="active")` → caller's asks (answers included). Agents call it at session start / when woken. With the human's token (no asks of their own) it returns everyone's; CLI `list --mine` does the same.
- `asks_list(status="active", agent=None)` → everyone's asks (read-only coordination view).
- `ask_get(id)` → ask + events.
- `ask_update(id, title=None, context=None, options=None, links=None, priority=None, ref=None)` → owner, open only.
- `ask_close(id, resolution="")` → owner or the human. Close after acting on the answer, with a one-line resolution of what was done.
- `ask_answer(id, answer)` → the human's token only (so the human can also answer from their own MCP client).
Errors come back as MCP tool errors with a clear message (not crashes).

Implementation: official `mcp` SDK 2.x (`mcp>=2.3,<3`; its `MCPServer` is FastMCP renamed), JSON responses, accepts both the initialize-handshake protocols (2025-xx) and 2026-07-28. `GET /mcp` → 405 (nothing is ever pushed, so no SSE stream). Extra tool arguments such as `agent` are ignored. DNS-rebinding Host checks are off: every request needs the bearer token, and clients may reach the server by a private IP.

## Notifications (Telegram)

When the human answers, closes or reopens an agent's ask, the single-tenant server posts to a Telegram chat so the asking agent wakes up (bots only see messages that @mention them, so the message starts with the agent's bot handle). Agent actions never notify (the agent knows what it did).

- env: `ASKS_NOTIFY=1` to enable (default off), `ASKS_TG_TOKEN_FILE` (file with the posting bot's token), `ASKS_TG_CHAT` (chat id, no default; without it every send is skipped and startup logs a warning), `ASKS_TG_HANDLES` (JSON `{"<slug>": "@bot_handle"}`, none built in; unknown agent → no mention, still post), `ASKS_TG_AGENT_CHATS` (JSON `{"<slug>": "<chat id>"}`, per-agent chat overrides)
- plain text, no parse_mode. Answer: `@handle Your ask #12 «title» was answered:\n<answer>\n\nAct on it, then close it: ask_close(12, "what was done")`. Close by the human: `@handle Your ask #12 «title» was closed: <resolution>`. Reopen by the human (e.g. an "Undo" right after "Not relevant"): `@handle Your ask #12 «title» was reopened: it is open again and waits for a decision`, or after an answer `…: the answer was withdrawn, it waits for a decision again`.
- `@` in the agent-written title gets a word joiner after it (`@\u2060`), so only the leading handle mentions a bot; the human's own answer/resolution is sent as written.
- sent in the background, in order; a network error, Telegram 5xx or 429 is retried after 5 s, 30 s and 2 min (429 waits at least Telegram's `retry_after`), other errors are not retried; the final failure is logged and never fails the API call; never log the bot token. With notifications on but the bot token file missing/empty, startup logs a warning.

## Web client

The single-tenant server serves a static web client from `ASKS_WEB_DIR` at `/` if that directory exists (not part of this repository), with `Cache-Control: no-cache` (revalidated via ETag) so a redeploy reaches a home-screen app at once. The client authenticates with the same bearer tokens and must never insert API data with `innerHTML` unescaped (titles and contexts come from agents).

## CLI (`cli/asks.py`, stdlib only)

`ASKS_URL` (default `http://127.0.0.1:8790`), token from `ASKS_TOKEN` or the file `ASKS_TOKEN_FILE` (default `~/.config/asks/token`).
Commands: `me`, `list [--status S] [--agent A] [--mine]`, `show ID`, `create --title T [--context C] [--option O]... [--link "title|url"]... [--priority urgent] [--ref R]`, `create --json FILE` (one object or a list), `update ID ...`, `answer ID --text T`, `close ID [--resolution R]`, `reopen ID`. Human-readable output; `--json` prints raw JSON. Non-zero exit on HTTP errors with the server's `detail`.

## Single-tenant deploy

- server env: `ASKS_DB` (SQLite file, default `data/asks.db`), `ASKS_TOKENS` (no default — unset means every request gets 401), `ASKS_WEB_DIR` (default `web/`), plus the notification and Drive vars
- run: `uvicorn server.app:app --host <private address> --port 8790 --no-access-log` under a process supervisor (launchd, systemd), restarted on exit
- tokens file chmod 600; give each agent only its own token, over a private channel
- agents register MCP: `claude mcp add --transport http asks http://<server>:8790/mcp --header "Authorization: Bearer <token>"`

## Drive storage (v2)

Goal: no private network on the phone and no server for the app. The **app talks only to Google** (Sign in with Google, data in the hidden **appDataFolder** of the signed-in user's Drive). The single-tenant **server stays** on a private network as the agents' **bridge** (REST/MCP/CLI/PWA unchanged, same tokens, same Telegram notifications) and mirrors every ask into the same appDataFolder. Public version (v4): same design, each user links their own Google account to the hosted bridge.

One Google Cloud project, Drive API enabled, OAuth consent: scopes `openid`, `email`, `https://www.googleapis.com/auth/drive.appdata`. Two OAuth clients in that project (appDataFolder is per project, so both see the same folder):
- **iOS client**, used by both the iOS and the macOS app. No secret. Redirect `com.googleusercontent.apps.<id-prefix>:/oauth2redirect` (the reversed client id).
- **Desktop app client** (client id + secret) for the bridge. Google doesn't allow `drive.appdata` in the device flow, so the bridge's account is linked once with the installed-app **loopback flow** (PKCE S256, `state` check, one-shot listener on `http://127.0.0.1:<free port>`, `access_type=offline`, `prompt=consent`), which yields the refresh token file `{"refresh_token": ...}`; link on a machine with a browser, then copy the file to the bridge host (chmod 600).

### Files in appDataFolder

One file per ask: name `ask-<id>.json` (e.g. `ask-17.json`), `mimeType: application/json`, `parents: ["appDataFolder"]`.

Content (UTF-8 JSON object) = the ask object exactly as REST returns it (every field of the `asks` table, `options`/`links` as arrays, timestamps ISO-8601 UTC with milliseconds and `Z`) plus:
- `"events"`: the full event list as `GET /api/asks/{id}` returns it (`id, ask_id, at, actor, kind, payload`), oldest first;
- `"rev"`: integer, +1 on every write by anyone (starts at 1);
- `"schema": 1`;
- `"writer"`: `"bridge"` or `"app"` (who wrote this revision).
Writers must keep fields they don't know (read-modify-write the JSON object, don't rebuild it from a struct).

File `appProperties` (set on every write, so a listing shows changes without downloading): `ask_id` (string), `status`, `agent`, `rev` (string).

### Who writes what

- **Bridge** is the only creator (ids stay sequential, from SQLite). It pushes the full ask + events after every successful write it performs (create, edit, answer, close, reopen — by agents or by the human via REST/MCP/CLI). Mapping `ask_id → Drive file id + last pushed rev` lives in SQLite (`drive_files` table). Before pushing it checks the remote `rev`: if the remote is newer than what it last saw, it pulls first (below), then pushes `remote rev + 1`.
- **App** (signed in = the human, role `denis`) only does the human's actions: answer, close, reopen. Read-modify-write: download the file fresh, apply the same state machine as the server (section "State machine": answer `open|answered → answered`, same answer again = no-op; close `open|answered → closed`, an open ask closed without resolution gets `Not relevant (Denis)`, `closed_by: "denis"`; reopen `closed|answered → open` clearing answer/answered_at/resolution/closed_by/closed_at), set `updated_at`, append an event `{id: max(event ids)+1, ask_id, at, actor: "denis", kind, payload}` with the same payloads as the server (`answered: {answer, previous_answer}`, `closed: {resolution, previous_status}`, `reopened: {previous_status, answer, answered_at, resolution, closed_by, closed_at}`), `rev + 1`, `writer: "app"`, and upload content + appProperties in one multipart `PATCH`. An invalid transition is shown like the server's 409 ("This question has already changed…") and the list refreshes.
- **Bridge pull** (background loop every 15 s, and before each push): list appDataFolder (`q: name contains 'ask-' and trashed = false`, fields `files(id,name,appProperties,modifiedTime)`, all pages); for every file whose `rev` is higher than the last seen, download it and **replay** the human's events that the bridge doesn't have yet (actor `denis`, kinds `answered|closed|reopened`, matched by `kind` + `at`) through the normal service as the human — so events, validation and Telegram notifications are exactly as if the human had used REST. Invalid replays (e.g. the agent closed it meanwhile) are logged and skipped. Then it pushes the resulting ask (`writer: "bridge"`), so the file converges to the bridge's state and event ids.
- Unknown files in appDataFolder are ignored. Nothing is ever deleted.

### Bridge config

Env: `ASKS_DRIVE=1` enables the mirror (default off: the bridge works exactly as before), `ASKS_DRIVE_CLIENT` (JSON file with the Desktop client's `client_id`, `client_secret`: flat, or as Google Cloud console downloads it, `{"installed": {...}}`), `ASKS_DRIVE_TOKEN` (JSON file with the refresh token, chmod 600). If Drive is enabled but unreachable or the token is missing/revoked, the bridge keeps serving from SQLite, logs a warning (never the token), and retries; REST/MCP calls never fail because of Drive. On first start with an empty `drive_files` table it pushes every existing ask (migration). Never log tokens. `ASKS_DRIVE_POLL` (seconds, default 15) sets the pull interval.

Bridge details (implementation notes, 31.10):
- A file is also re-read when its `modifiedTime` differs from the one the bridge last wrote or read, even if `rev` did not grow: that catches the app and the bridge both writing rev N+1 from the same base. The bridge then pushes the highest rev it has seen + 1.
- Each app action is replayed at most once: the bridge remembers the replayed or skipped `kind` + `at` per ask (`drive_replayed` table), so an app event still in the file after a failed push is not applied twice. The replayed event gets the bridge's own `id` and `at`; after the next push the app's original event is no longer in the file (the app must not rely on its event's `id`/`at` surviving).
- An existing `ask-<id>.json` without a mapping is adopted, never duplicated (before creating a file the bridge looks it up by name). If its `agent` + `created_at` don't match the SQLite ask (e.g. the database was restored from an old copy), nothing is replayed from it and it is overwritten (`rev + 1`). Several files with one name: the mapped one, else the highest `rev`, wins; the others are ignored. A mapped file that disappeared (404 or trashed) is written again as a new file (its `rev` starts at 1 again). A file whose content is not a JSON object is overwritten from SQLite.
- Top-level fields the bridge doesn't know are kept on its writes (remembered from its last read); `events` are always the bridge's own list.

## Tasks (v3)

v3 adds **tasks**: long-running work an agent does itself (more than a day, or waiting on someone outside), so agents don't forget it and the human sees who is doing what. The human only **reads** tasks; they never wait for the human's decision (that is what asks are for).

### Data model

Table `tasks`:

| field | type | notes |
|---|---|---|
| id | int PK | autoincrement, own sequence (task #3 ≠ ask #3) |
| agent | text | owner, from the token (never from the body) |
| title | text | required, 1–200 chars: what is being done ("Get the supplier to confirm the new fabric") |
| context | text | optional, ≤ 4000, plain text like asks |
| status | text | `active` (agent is working on it) · `waiting` (blocked on someone/something outside) · `done` · `dropped` |
| waiting_on | text | optional, ≤ 200: who/what it waits for ("Supplier support reply"); kept when status changes, cleared only explicitly with `""` |
| next_check | text | optional date `YYYY-MM-DD`: when the agent will look again |
| links | json array of `{title, url}` | optional, ≤ 10, same rules as asks |
| ref | text | optional, ≤ 100 |
| resolution | text | optional, ≤ 1000, set by done/drop |
| created_at, updated_at, finished_at | text | ISO-8601 UTC with ms and `Z`; `finished_at` set by done/drop, cleared by reopen |

All text cleaning rules from asks apply. Table `task_events`: `id, task_id, at, actor, kind (created|edited|status|done|dropped|reopened), payload (json)`.

State: `active ⇄ waiting` (via update), `active|waiting → done|dropped` (done/drop, optional resolution), `done|dropped → active` (reopen). Only the **owner agent** writes its tasks (create, update, done, drop, reopen); the human's token can read everything and write nothing (403). No Telegram notifications for tasks.

### REST

- `GET /api/tasks?status=open|active|waiting|done|dropped|finished|all&agent=<slug>&limit=<n>` → `{"tasks": [...], "counts": {"active": n, "waiting": n, "done": n, "dropped": n}}`. `open` (default) = active + waiting; `finished` = done + dropped (most recent 100 by default). Order: open ones by `next_check` ascending (nulls last), then `created_at` ascending (oldest = longest-running first); finished by `finished_at` desc.
- `GET /api/tasks/{id}` → task + `events`.
- `POST /api/tasks` (agent) `{title, context?, status?: active|waiting, waiting_on?, next_check?, links?, ref?}` → 201.
- `PATCH /api/tasks/{id}` (owner, not finished) any subset of `title, context, status (active|waiting), waiting_on, next_check, links, ref`; `null` = unchanged, `""`/`[]` clears.
- `POST /api/tasks/{id}/done` and `/drop` `{resolution?}`; `POST /api/tasks/{id}/reopen`. Errors as for asks (401/403/404/409/422).

### MCP

`task_create(title, context="", status="active", waiting_on="", next_check="", links=[], ref="")`, `tasks_mine(status="open")`, `tasks_list(status="open", agent=None)`, `task_get(id)`, `task_update(id, title=None, context=None, status=None, waiting_on=None, next_check=None, links=None, ref=None)`, `task_done(id, resolution="")`, `task_drop(id, resolution="")`, `task_reopen(id)`. Docstrings teach: create a task for work that will take more than a day or waits on someone outside; one task per piece of work; set `waiting_on` + `next_check` when blocked; check `tasks_mine()` at session start / when woken and update what moved; finish with a one-line resolution. A question that needs the human's decision is an **ask**, not a task. Agents restricted to a tool subset should get `task_create/tasks_mine/task_get/task_update/task_done` too.

### CLI

`tasks [--status S] [--agent A] [--mine]`, `task show ID`, `task create --title T [--context C] [--waiting-on W] [--next-check YYYY-MM-DD] [--status waiting] [--link "title|url"]... [--ref R]`, `task update ID ...`, `task done ID [--resolution R]`, `task drop ID [--resolution R]`, `task reopen ID`.

### Drive

The bridge mirrors tasks exactly like asks: file `task-<id>.json` in the same appDataFolder, content = REST task object + `events` + `rev` + `schema: 1` + `kind: "task"` + `writer: "bridge"`; appProperties `task_id`, `status`, `agent`, `rev`. Only the bridge writes task files (the app never writes them), so there is no replay for tasks. Ask files get no new fields. The app lists `name contains 'ask-' or name contains 'task-'` in one `files.list` (or two — implementer's choice) and caches both.

### Bridge implementation notes (v3, 31.10)

Resolved where the contract above was silent; the app may rely on these.
- **Events.** Task events carry `task_id` (not `ask_id`): `{id, task_id, at, actor, kind, payload}`, ids from their own sequence. Payloads: `created` = the cleaned fields (`title, context, status, waiting_on, next_check, links, ref`); a `PATCH`/`task_update` that changes anything writes **one** event, kind `status` if the status changed (active ⇄ waiting), otherwise `edited`, payload `{"changes": {field: {"from", "to"}}}` (same shape as asks' `edited`; a no-op patch writes nothing); `done` / `dropped` = `{resolution, previous_status}`; `reopened` = `{previous_status, resolution, finished_at}`. `actor` is always the owner agent.
- **Reopen clears `resolution` too** (with `finished_at`; both stay in the `reopened` event), as reopen does for asks: an active task never shows an old result. `waiting_on` and `next_check` are kept.
- `status` in create/update accepts only `active|waiting` (422 otherwise; finishing is done/drop). `next_check` must be a real calendar date `YYYY-MM-DD` (422 otherwise); a past date is allowed (= overdue). `""` clears `context`, `waiting_on`, `next_check`, `ref`; `[]` clears `links`.
- `limit` defaults to 200, and to 100 when only finished tasks are asked for (`done`, `dropped`, `finished`). `all` lists open ones first (their order), then finished ones (theirs).
- `tasks_mine` / CLI `tasks --mine` with the human's token return everyone's tasks, like `asks_mine`. The human's token on any task write → 403; another agent's token → 403.
- Drive: the bridge lists ask and task files in one `files.list`: `(name contains 'ask-' or name contains 'task-') and trashed = false` (Drive matches `name contains` by prefix, so `'ask-'` alone does not find `task-` files). Task mappings live in their own table `drive_task_files` (same columns as `drive_files`, which is unchanged), so a v2 database needs no row migration; existing tasks are pushed on the first sync. A task file changed by someone else is logged and overwritten (`rev + 1`); unknown top-level fields are kept, as in ask files. Adopt / recreate / duplicate-name rules are the same as for ask files.

## Hosted bridge and agent connection (v4)

Goal: any user installs the app, signs in with Google (data in their own Drive appDataFolder), then connects their own agents to Qtracker by pasting **one URL** into their MCP client — no hand-copied tokens — following the **MCP Authorization spec, revision 2026-07-28** (read it: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization).

### Shape

- **Hosted mode** of the same server package (`ASKS_MODE=hosted`; the single-tenant mode stays the default and must keep working untouched). Public base URL from env `ASKS_PUBLIC_URL` (e.g. `https://qtracker.example.com`), MCP at `/mcp` (streamable HTTP, stateless). Runs on Google Cloud Run, several instances, no local disk state.
- **Data of record = the user's Drive appDataFolder**, same file format as v2/v3 (`ask-<id>.json`, `task-<id>.json`, `writer: "bridge"`). In hosted mode the bridge applies every write directly to the Drive file (read-modify-write, the same validation/state machine as `core.py`/`tasks.py`; reuse that code, don't fork the rules), so the app and the hosted bridge are two writers of the same files. New ids: next free number for that user, allocated through a per-user counter in the metadata store (transactional), initialised from the highest id found in Drive.
- **Metadata store = Firestore** (behind a small interface with an in-memory implementation for tests): users (Google `sub`, email, encrypted Google refresh token, id counters), agents (per user: id, display name → slug used as the `agent` field, client name, created, last seen, webhook URL, revoked), OAuth clients/registrations, authorization codes, access/refresh tokens (store hashes only), agent keys (hashes). Google refresh tokens are encrypted at rest (envelope key from env/Secret Manager; KMS later).
- **Authorization server** at the same origin: `/.well-known/oauth-protected-resource` (RFC 9728, also referenced from `WWW-Authenticate` on 401), `/.well-known/oauth-authorization-server` (RFC 8414) advertising `client_id_metadata_document_supported: true`; `/authorize`, `/token`, `/register` (DCR, deprecated in the spec but Claude and others still use it), `/revoke`. Client ID Metadata Documents (client_id is an https URL → fetch and validate the metadata document, cache it, SSRF-safe fetch), PKCE S256 required, RFC 8707 `resource` parameter required and checked (tokens audience-bound to `ASKS_PUBLIC_URL/mcp`), RFC 9207 `iss` in authorization responses, redirect URI rules from the spec (exact match; loopback any port for native/CLI clients). Short-lived access tokens (1 h) + rotating refresh tokens.
- **Login = Google as the upstream IdP.** `/authorize` → our page → Google OAuth (the project's **Web application** client; env `ASKS_GOOGLE_WEB_CLIENT` JSON with client_id/secret; redirect `ASKS_PUBLIC_URL/oauth/google/callback`) with scopes `openid email https://www.googleapis.com/auth/drive.appdata`, `access_type=offline` → callback → **consent page** (ours, simple, English, mobile-friendly, no external assets): "<client name> wants to ask you questions and track its tasks in Qtracker. Agent name: [input, prefilled from the client name]" + Allow / Cancel → issue the code to the MCP client. Same Google account as in the app ⇒ same appDataFolder (same project).
- **Identity in tools**: the access token resolves to (user, agent). All v1/v3 MCP tools work unchanged, scoped to that user's Drive; `agent` = the agent's slug. The user themselves (role `denis` in v1 terms) is the app; MCP tokens are always agent tokens.
- **Agent keys (fallback)** for clients without OAuth: `Authorization: Bearer qtk_<random>`; created/revoked from the app; behave exactly like an OAuth-connected agent.
- **App API** (for build 4: Connect an agent, Connected agents): authenticated with the user's Google **ID token** (audience = the iOS client id; verify signature via Google certs, `iss`, `aud`, `exp`). `GET /api/agents`, `PATCH /api/agents/{id}` (name, webhook_url), `DELETE /api/agents/{id}` (revoke: all its tokens/keys die), `POST /api/agents/keys` (create an agent key; returned once), `GET /api/connect-info` (server URL + per-client snippets).
- **Wake-up**: when the user answers/closes/reopens an ask in the app, the hosted bridge can't see it instantly (the app writes Drive directly). So: a per-user change poller is out of scope for v4; instead the **app** calls `POST /api/events` (ID-token auth) after each action with `{ask_id, kind}`, and the bridge then POSTs to that agent's webhook URL (JSON `{type, ask, answer?, resolution?}`, signed with an HMAC header, retries like notify.py). Agents also see answers via `asks_mine`. Telegram pings like the single-tenant bridge's can be built as a webhook receiver.
- **Security**: no tokens in logs; rate limits on auth endpoints; CORS closed; consent page has CSRF protection; state/nonce checks with Google; only https redirect URIs except loopback.

### Out of scope for v4

Migrating single-tenant users, the Google verification / External consent switch, the app UI (build 4 consumes the App API above).

### Hosted bridge implementation notes (v4, 31.10)

Resolved where the contract above was silent; the app (build 4) and agents may rely on these. Code: `server/hosted/`; deploy: `deploy/hosted/README.md`. Updated the same day after an independent security review (points marked *review*).
- **Shared rules.** Validation, permissions and both state machines live in `server/rules.py` and `server/task_rules.py` as pure functions; `core.py`/`tasks.py` (SQLite) and `hosted/records.py` (Drive files) both apply them, so errors (401/403/404/409/422) are identical. Hosted only adds 503 `unavailable` (Drive unreachable, or the user's Google grant missing/revoked, with the link below) and 429.
- **Hosted endpoints for agents**: `/mcp` and the same REST as v1/v3 (`/api/me`, `/api/asks…`, `/api/tasks…`) with the same tokens. MCP server name `qtracker`; no `ask_answer` (agents never answer); tool texts say "your user" instead of "your human"; the server instructions tell agents to call `asks_mine` and `tasks_mine` at session start. An unexpected server error reaches the agent as `server error (<ExceptionType>); try again later` without its message (*review*). The PWA (`web/`) is not served in hosted mode.
- **Scope and audience.** One scope, `qtracker` (unknown requested scopes are ignored; the token response always says `qtracker`). `resource` is required at `/authorize` (scheme/host case, a default port and a trailing slash are normalised); at `/token` it may be omitted (the code / refresh token is already bound), but if sent it must match (`invalid_target`). `ASKS_PUBLIC_URL` is canonicalised at startup (lower-case scheme/host, no default port, no trailing slash), since issuer and audience are compared as exact strings (*review*).
- **401** from `/mcp` and the agents' REST: `WWW-Authenticate: Bearer resource_metadata="<ASKS_PUBLIC_URL>/.well-known/oauth-protected-resource/mcp", scope="qtracker"`, plus `error="invalid_token"` when a token was sent. The protected resource metadata is served at that path and at the root path (same document). CORS is closed everywhere (no `Access-Control-*`, preflights get 405).
- **No redirect before consent** (*review*): DCR/CIMD redirect URIs are chosen by whoever registered them, so the browser goes there only after the user answered our consent page (Allow → code, Cancel → `access_denied`). A malformed authorization request (response_type, PKCE, resource, state) gets our error page (400). A failure at the Google step (cancelled, Drive box unticked, Google error) gets our page with a "Return to <host>" link carrying `error`, `state` and `iss`; the user follows it or not.
- **Redirect URIs**: https, or http on `127.0.0.1`, `[::1]` or `localhost` (any port at request time, RFC 8252 §7.3); everything else exact string match. Custom schemes (`cursor://…`) are rejected, as the MCP spec requires, so such clients can't connect. The localhost warning on the consent page follows the redirect URI actually used (*review*).
- **DCR**: `token_endpoint_auth_method` defaults to `client_secret_basic` (RFC 7591); `none`, `client_secret_post`, `client_secret_basic` accepted, `private_key_jwt` not. Client ids `qtc_` + 22 URL-safe characters (other ids are unknown without a lookup); secrets stored as SHA-256 and accepted by Basic or form. Every client may use the refresh grant. A registration nobody signs in with expires after 24 h; a sign-in or refresh keeps it for 180 days (*review*). 10 registrations/min per address.
- **CIMD**: the client id URL must be https on port 443 with a path, no fragment, user info or dot segments, on a public host. Fetch: `Accept-Encoding: identity`, any `Content-Encoding` refused, no redirects, 5 s wall-clock deadline for the whole exchange, ≤ 5 KB read raw, JSON content type, connect only to public addresses (every resolved address checked, each tried in turn), cached 5 min–24 h by `Cache-Control`; a failure is answered from memory for 60 s. At most 4 documents are fetched at once per instance, on their own threads; when all 4 are busy the request fails at once (503 page / `temporarily_unavailable`) (*review*). The document needs `client_id` (= URL), `client_name`, `redirect_uris`; `token_endpoint_auth_method` absent or `none`. The consent page shows the document's host; for DCR clients it says the name is unverified; the redirect host is always shown. Client and agent names lose control, format and bidi characters (*review*).
- **Google leg**: `prompt=select_account`; if Google returns no refresh token and none is stored for that `sub`, or `drive.appdata` was not granted, the bridge asks once more with `prompt=consent` + `login_hint`, then shows the page above. The ID token is verified (signature, `iss`, `aud` = Web client, `exp`, `nonce`). The pending authorization (10 min) is bound to the browser by a `__Host-` cookie named after it, so two tabs can connect two agents at once; the consent form carries a CSRF token and is single use.
- **Agents.** Name 1–40 chars; slug = lower-case ASCII with `-` (Cyrillic transliterated), fixed at creation (renaming changes only the display name). Names with nothing left in ASCII get `agent-<6 hex of the name>` (different names stay different agents); `denis` is reserved, so "Denis" becomes `denis-agent` (*review*). A client connects as an existing agent of the same slug only if it connected as that agent before; otherwise the consent page asks "Connect as <name>?" and needs the explicit "Connect as existing agent <name>" button (form field `confirm` = the slug), and the name prefilled for a new client never collides with an existing agent ("Research Bot 2") (*review*). The agent's original `client_id`/`client_name` never change; every client that connected as it is listed in `clients`. Agent ids `ag_` + 16 characters.
- **Tokens**: access `qta_` + 43 characters, 1 h; refresh `qtr_…`, 60 days from its issue, rotated on every use; codes 120 s; agent keys `qtk_` + 40 characters; all opaque, stored as SHA-256. A bearer not in the `qta_`/`qtk_` format is refused without a store lookup (*review*). A used code or refresh token presented again revokes its whole family: a `families/{id}` mark is written first and checked on every use, refresh and issuance, so tokens a concurrent request writes after the sweep are dead too (*review*). `/revoke` (RFC 7009, client auth as at `/token`) revokes the whole grant; revoking an agent kills all its tokens and keys at once.
- **Agent keys**: `POST /api/agents/keys {name, existing?}` → 201 `{agent, key, drive_linked, link_url}`; the key is shown only there. A name of an existing agent → 409, unless `existing: true` adds the key to that agent (*review*). A key needs the user's Google grant at the bridge, which normally comes from an OAuth connection; without one `drive_linked` is false and `link_url` = `ASKS_PUBLIC_URL/oauth/link` (Google sign-in only; stores the refresh token; also heals a revoked grant). `GET /api/connect-info` returns `server_url`, `drive_linked`, `link_url` and `clients` snippets.
- **App API shapes**: agent = `{id, name, slug, client_name, client_id, clients: [{client_id, client_name, connected_at}], has_key, key_prefix, created_at, last_seen, webhook_url, webhook_secret_set, revoked_at}`; `GET /api/agents` → `{agents}` (active only); `PATCH {name?, webhook_url?, rotate_webhook_secret?}` (unknown fields 422; webhook URL https on port 443 on a public host) → `{agent, webhook_secret?}`, the secret (`whsec_…`) shown once when first set or rotated and stored encrypted; `DELETE` → `{agent}` with `revoked_at`.
- **`POST /api/events {ask_id, kind}`**: 404 if the ask isn't in the user's Drive, 409 if its status doesn't match the kind (`answered`→answered, `closed`→closed, `reopened`→open), else 202 `{queued: n}`. Webhook body `{type: "ask.<kind>", ask (no events), answer? (answered), resolution? (closed), event: {id, at} (latest event of that kind), delivery_id, sent_at}`; headers `Qtracker-Event`, `Qtracker-Delivery`, `Qtracker-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>` (receivers check it in constant time and reject old `t`). Delivery (*review*): 4 worker threads per instance, at most one delivery in flight per receiver host, 10 s wall-clock per attempt, `Accept-Encoding: identity`, at most 4 KB of the reply read raw and dropped; network errors, 5xx and 429 rescheduled after 5 s, 30 s, 2 min (429: at least `Retry-After`, capped at 5 min), never slept on in a worker; at most 1000 waiting deliveries (more are dropped and logged). In memory: lost if the instance stops.
- **Drive writes**: read-modify-write of the file; just before the upload the bridge re-reads the file's `modifiedTime` and starts over (3 attempts, then 409) if someone else wrote it (Drive has no conditional writes, so a write landing in the last milliseconds can still be lost). New event id = max + 1, `rev` + 1, `writer: "bridge"`, unknown top-level fields kept. New ids: per-user counter in Firestore, initialised from the highest id in Drive; before creating `ask-<n>.json` the bridge looks the name up and, if it exists (e.g. a single-tenant bridge writes to the same account), moves the counter past the highest id in Drive.
- **Drive reads are bounded** (*review*): listings ask for `size` and stop at 5000 ask/task files; files over 256 KB are skipped as unreadable (by `size`, and again while reading); downloads say `Accept-Encoding: identity`, refuse compressed bodies and read raw up to the cap. Counts come from `appProperties`. A list downloads only the files its `limit` can need: when more than `limit` + 50 files match, they are first ordered from `appProperties` alone (status block; open ones by id, others by `modifiedTime`, latest first), so for very long lists the order of items whose last write isn't their answer/close can be approximate. The per-instance cache holds validated records only (32 MB budget).
- **Rate limits** (per instance; client IP = Cloud Run's last `X-Forwarded-For` entry, IPv6 counted per /64): `/authorize` + `/oauth/link` 30/min, Google callback + consent 30/min, `/token` 60/min, `/register` 10/min, `/revoke` 30/min, App API 120/min per user; `/mcp` and agents' REST 300/min per agent and 1200/min per user, and 60 failed bearer checks/min per address (after that 429 before any lookup). When the limiter's table is full, idle keys go first, then the least recently used (*review*).
