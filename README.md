# Qtracker server

Qtracker lets people keep track of **asks** and **tasks** between themselves and their AI agents. An agent that needs a decision puts an ask on its human's list instead of asking in a chat; the human answers with one tap, and the agent picks the answer up the next time it checks. Agents also record their own long-running work as tasks, so they don't forget it and the human sees who is doing what.

This repository is the server: a hosted **MCP server** for agents plus an **app API** for the Qtracker apps. The data of record lives in each user's own Google Drive, in the hidden per-app `appDataFolder`; the server keeps only accounts, connected agents and tokens (in Firestore).

- Agents connect with one URL (`https://<host>/mcp`) using MCP authorization (OAuth 2.1 with PKCE, client ID metadata documents or dynamic client registration), signing the user in with Google. Clients without OAuth can use an agent key created in the app.
- The official instance runs at `https://qtracker.allover.art`.
- The Qtracker iOS/macOS app is a separate, proprietary client and is not part of this repository.

### Self-hosting

The server runs on Google Cloud Run with Firestore; the step-by-step guide is [deploy/hosted/README.md](deploy/hosted/README.md). A self-hosted server needs **its own Google Cloud project, its own Google OAuth clients and its own app/clients**: Drive's appDataFolder is scoped to one Google Cloud project, so data written through your project is invisible to apps registered in another project (including the official Qtracker apps), and the reverse.

The contract (fields, statuses, REST, MCP, Drive file format, OAuth details) is [SPEC.md](SPEC.md). Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md). License: [Apache-2.0](LICENSE). The license does not grant rights to the Qtracker name and logo (`server/hosted/static/`); a public instance of your own should use its own name and icons.

## Concepts

Asks: `open` (waits for the human) → `answered` (the agent acts on the answer) → `closed`. The agent closes its ask with a one-line resolution of what was done; the human can also close an open ask as not relevant, and either can reopen it. The full history, including earlier answers, stays in the ask's events.

Tasks: `active` (the agent is working on it) ⇄ `waiting` (blocked on someone outside; `waiting_on` says who, `next_check` when to look again) → `done` or `dropped`, with a one-line result. Only the agent that owns a task writes it; the human only reads tasks. Anything that needs the human's decision is an ask, not a task.

MCP tools: `ask_create`, `asks_mine`, `asks_list`, `ask_get`, `ask_update`, `ask_close`, and `task_create`, `tasks_mine`, `tasks_list`, `task_get`, `task_update`, `task_done`, `task_drop`, `task_reopen`. The server instructions tell agents to call `asks_mine` and `tasks_mine` at the start of every session and when woken.

## Layout

```
server/              FastAPI app: REST /api, MCP /mcp (uvicorn server.app:app)
server/hosted/       hosted multi-tenant mode: OAuth server, agents and tokens, Drive records, app API, webhooks
server/tests/        pytest suite, no network (fake Google, Drive and Firestore)
cli/asks.py          stdlib-only CLI for the single-tenant bridge's REST API
deploy/hosted/       Dockerfile, Cloud Build config and the Cloud Run guide
```

## Two modes

**Hosted** (`ASKS_MODE=hosted`): many users, each signs in with Google; asks and tasks are read and written directly in the user's Drive appDataFolder, with the same validation and state machines as the single-tenant mode (`server/rules.py`, `server/task_rules.py`). This is what the Cloud Run guide deploys.

**Single-tenant bridge** (default, no `ASKS_MODE`): one human and their agents, data in a local SQLite file, static bearer tokens. Optionally it posts Telegram notifications when the human answers, and mirrors everything into one Google account's Drive appDataFolder. It is meant for a private network; it has no OAuth.

## Single-tenant bridge

### Run

Dependencies only through [uv](https://docs.astral.sh/uv/):

```bash
mkdir -p _tmp/dev
python3 - <<'EOF'
import json, secrets
tokens = {secrets.token_urlsafe(24): {"role": "denis"},  # the human (legacy role id, see SPEC.md)
          secrets.token_urlsafe(24): {"role": "agent", "agent": "research-bot"}}
open("_tmp/dev/tokens.json", "w").write(json.dumps(tokens, indent=2))
EOF
chmod 600 _tmp/dev/tokens.json
ASKS_DB=_tmp/dev/asks.db ASKS_TOKENS=_tmp/dev/tokens.json \
  uv run --python 3.11 --with-requirements server/requirements.txt \
  python -m uvicorn server.app:app --host 127.0.0.1 --port 8790
```

Tokens file (`ASKS_TOKENS`): `{"<token>": {"role": "denis"}, "<token>": {"role": "agent", "agent": "<slug>"}}`; tokens are at least 16 characters, slugs are lower-case `[a-z0-9_-]`. The role id `denis` means "the human" (a legacy identifier that is also written into the data files). The server re-reads the file when it changes, so adding or revoking a token needs no restart; if an edited file stops parsing, the server stops accepting **all** tokens after 5 seconds (and logs an error) until it is fixed, so a revoked token can't survive a stray comma. Every agent gets its own token: the server takes the identity from the token, so one agent can't act as another.

Environment (see `server/config.py`):

| variable | meaning |
|---|---|
| `ASKS_DB` | SQLite file (default `data/asks.db`) |
| `ASKS_TOKENS` | tokens file; without it every request gets 401 |
| `ASKS_WEB_DIR` | optional static web client served at `/` (default `web/`, not included in this repository) |
| `ASKS_NOTIFY` | `1` enables Telegram notifications (default off) |
| `ASKS_TG_TOKEN_FILE` | file with the bot token that posts them |
| `ASKS_TG_CHAT` | chat id to post to (no default) |
| `ASKS_TG_HANDLES` | JSON `{"<agent slug>": "@bot_handle"}`: the handle each message starts with, so that agent's bot wakes up |
| `ASKS_TG_AGENT_CHATS` | JSON `{"<agent slug>": "<chat id>"}`: per-agent chat overrides |
| `ASKS_DRIVE`, `ASKS_DRIVE_CLIENT`, `ASKS_DRIVE_TOKEN`, `ASKS_DRIVE_POLL` | Drive mirror, see below |

When the human answers, closes or reopens an ask, the notification goes to the chat with the agent's handle in front ("@bot Your ask #12 «…» was answered: …"); agents' own actions never notify. `@` in agent-written titles is defused, so only the leading handle mentions a bot. Failed sends are retried after 5 s, 30 s and 2 min (429 waits as long as Telegram asks) and never fail the API call.

Back up the SQLite file with SQLite's online backup (e.g. `sqlite3 asks.db ".backup asks-copy.db"`): it is safe while the server runs.

### Connect an agent

```bash
claude mcp add --scope user --transport http asks http://<server>:8790/mcp \
  --header "Authorization: Bearer $(cat ~/.config/asks/token)"
claude mcp list        # asks should be connected
```

Put `--header` after the URL, otherwise it swallows the name and URL as extra headers. Rules for agents (also in the tool descriptions): one decision per ask, with a title the human can answer in one tap and 2–5 lines of context; update an open ask instead of creating a duplicate; after acting on an answer, `ask_close(id, "what was done")`.

### CLI

```bash
alias asks='python3 cli/asks.py'
asks me
asks list --mine                       # my open + answered asks, with answers
asks list --status all --agent research-bot
asks show 12                           # context, options, links, answer, history
asks create --title "Ship X today?" --context "2–5 lines" --option "Yes" --option "No" \
            --link "Plan|https://example.com/plan" --ref "chat 5487"
asks update 12 --option "Yes" --option "Later"   # replaces the whole list of options
asks close 12 --resolution "shipped X"
asks answer 12 --text "Yes"            # the human's token only
asks tasks --mine                      # my active + waiting tasks, nearest check first
asks task create --title "Get the supplier to confirm the new fabric" --status waiting \
                 --waiting-on "Supplier support reply" --next-check 2026-11-03
asks task done 3 --resolution "Supplier confirmed"
asks --json list                       # raw JSON
```

The server address comes from `ASKS_URL` (default `http://127.0.0.1:8790`), the token from `ASKS_TOKEN` or the file `ASKS_TOKEN_FILE` (default `~/.config/asks/token`). On an HTTP error the CLI exits with code 1 and prints the server's `detail`. `--context -` and `--text -` read stdin; `create --json FILE` takes one object or a list.

### Drive mirror

With `ASKS_DRIVE=1` the bridge writes every ask and task into the appDataFolder of one linked Google account (`ask-<id>.json`, `task-<id>.json`) and every 15 s (`ASKS_DRIVE_POLL`) replays the human's answers, closes and reopens made by an app directly in those files. The format and rules are in SPEC.md, "Drive storage". It needs a **Desktop app** OAuth client of your Google Cloud project (`ASKS_DRIVE_CLIENT`: its JSON as the console downloads it, or flat `{"client_id", "client_secret"}`) and a refresh token for the `https://www.googleapis.com/auth/drive.appdata` scope obtained with the installed-app loopback flow (`ASKS_DRIVE_TOKEN`: `{"refresh_token": "..."}`, chmod 600). If Google is unreachable or the token is revoked, the API keeps working from SQLite, a warning is logged (never the token) and the mirror catches up later; a new token file is picked up without a restart. Nothing is ever deleted from Drive.

## Development

```bash
uv run --python 3.12 --with-requirements server/requirements.txt --with-requirements deploy/hosted/requirements.txt \
  --with httpx --with pytest python -m pytest server/tests -q
```

The tests need no network and no Google Cloud: Google, Drive and Firestore are faked in memory, and the hosted tests include an end-to-end run of the official MCP client with its OAuth flow. CI runs the same command on every pull request.
