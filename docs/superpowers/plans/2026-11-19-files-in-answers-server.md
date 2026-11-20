# Files in answers — server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The hosted bridge accepts `answer_files` from the apps, shows them to the owner agent, streams normal files from the user's Drive through `/media/v1/<token>`, serves secret files once through a bearer route with a claim state machine, deletes files on close/reopen, and sweeps hourly.

**Architecture:** Mirrors voice answers (section 5): validation in a new `server/hosted/files.py` (like `voice.py`), routes in `server/hosted/files_api.py` (like `voice_api.py`), streaming in `DriveClient.stream_media`, the secret claim in the metadata store (like `UploadGrants.claim`), the sweep as an OIDC-authenticated internal route called by Cloud Scheduler. Apps (web/Android, Apple) are separate plans after this one ships.

**Tech Stack:** Python 3.12, FastAPI/Starlette (ASGI), uvicorn on Cloud Run, Firestore metadata store, Google Drive v3 (appDataFolder), pytest with `server/tests/fake_drive.py`.

**Spec:** `SPEC.md` section 7 «Files in answers» (7.1–7.10, Order step 7). Read 7.2, 7.4 and 7.6 in full before any task; every value below is copied from there.

## Global Constraints

- Constants in one place (`server/hosted/files.py`): `MAX_FILES = 6`, `MAX_FILE_BYTES = 104_857_600`, `MAX_SECRET_BYTES = 10_485_760`, `SECRET_TTL = 24 h`, `SECRET_ORPHAN = 1 h`, `FILE_ORPHAN = 24 h`, `REQUEST_TIMEOUT = 900`, claim lease = `REQUEST_TIMEOUT + 60` s, link TTL 3600 s, AEAD context `"file"`, claims `{user, agent, ask_id, answered_at, file_id, n, exp}`.
- Hosted only. The single-tenant bridge always returns `answer_files: []` (`server/db.py:93` pattern).
- Nothing of a file in logs, webhooks, push or events beyond ids: no names, no content, no links, no tokens. Drive error text through `DriveClient._scrub`.
- Download headers exactly: `Content-Type: application/octet-stream`, `Content-Disposition: attachment; filename="ask-<id>-file-<n>[.<ext>]"; filename*=UTF-8''<pct-encoded name>`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`, `Cache-Control: private, no-store`, `Content-Length` = `bytes`.
- Budgets: bucket `files` 20/min per user+agent; byte bucket `file_bytes` 1 GiB/hour per agent, charged at download start, refused requests charge nothing.
- Deletes are `files.delete` (permanent), only for a Drive file whose appProperties say `kind: "file"` and this `ask_id`.
- Existing voice/media routes, tokens and tests stay byte-for-byte (`voice.py` CLAIMS untouched).
- UI/agent texts in English, exactly as the spec quotes them; code comments as in the repo.
- One MCP tool for bytes only: `ask_secret_file` (spec 7.8, owner's decision 19.11, ask 1346) — Task 10. No MCP tool for normal files.

## Review Focus

1. A re-answer (Change answer) while an agent is mid-download of a dropped file: the stream ends with an error and the next request is 410, never a 500 and never the new answer's file.
2. Two agents' requests for the same secret file at the same instant (two instances): exactly one gets the bytes, the other 409, then 410 forever — the claim transaction, not timing, decides.
3. A Drive stream that is longer than `bytes` (file replaced under the same id): the response is aborted at `bytes`, `downloaded_at` is not written.
4. The sweep meeting a user whose Drive grant is revoked: that user is skipped with an id-only log line and the slice continues; the cursor still advances.
5. An ask edited by hand in the store with a 7th item, a name with bidi controls, or `file_id` set together with `deleted_at`: reads sanitise or drop the item, never serve it.

Each line has a test in the task that owns the code (Tasks 5, 6, 5, 8, 1).

---

### Task 0: Spikes (7.10) on a throwaway Cloud Run service

**Files:** a throwaway folder outside the repository (nothing in the repo): `app.py` (a tiny ASGI app), `REPORT.md`.

The live bridge is not touched. A separate service `askhooman-spike` in the same project (`--memory=512Mi`, uvicorn HTTP/1, like `deploy/hosted/Dockerfile`), deleted at the end of the task.

- [ ] **Step 1:** Route `GET /blob?mib=100` streams N MiB of pseudo-random bytes in 1 MiB chunks with `Content-Length`; route `GET /once` sends 10 MiB as one response through a `_Held`-style wrapper that logs (to stdout, ids only) whether `http.disconnect` arrived and whether the final `send` returned.
- [ ] **Step 2:** Spike 1: download `/blob?mib=100` with `curl --limit-rate` at 2 and 5 Mbit/s; kill at 50 % and 99 %; deploy once with the default timeout and once with `--timeout=900`; kill `/once` at 99 % ten times. Record: does a streamed 100 MiB body with `Content-Length` pass (yes/no); the timeout seen; how many of ten killed `/once` runs the wrapper reported as complete.
- [ ] **Step 3:** Spike 3: a Cloud Scheduler job (or `gcloud auth print-identity-token --impersonate-service-account=<bridge SA> --audiences=<url>`) calls `POST /oidc`; the route runs `GoogleOAuth.verify_id_token(token, [<url>])` copied from `server/hosted/google.py:171` and prints `iss`, `aud`, `email`. Record: accepted as is, or what differs.
- [ ] **Step 4:** Spike 2: a static page served from `http://localhost` (then from Safari, and Chrome on Android over the LAN) uploads a 50 MiB file to Drive appDataFolder with the user's token: `POST …?uploadType=resumable`, `PUT` 8 MiB chunks, cut the connection, resume with `Content-Range: bytes */<total>`. Record per browser: CORS ok, 308 + `Range` readable, resume ok.
- [ ] **Step 5:** Write `REPORT.md`: one line per «Decides» item of 7.10 with the measured answer and the design consequence; delete the spike service and the Scheduler job; ledger a `Ruling:` for each consequence that changes a later task (e.g. chunked without `Content-Length` over 32 MiB → Task 5).

### Task 1: The data model — `answer_files` validation and names

**Files:**
- Create: `server/hosted/files.py` (constants, `clean_name`, `clean_files`, `download_name`)
- Modify: `server/rules.py` (answer path accepts `files` beside `voice`), `server/db.py:93` (single tenant `[]`)
- Test: `server/tests/test_hosted_files.py`

**Interfaces:**
- Produces: `clean_name(raw: str) -> str`; `clean_files(value, answered_at: str | None, status: str) -> list[dict]` (returns the readable list per 7.2: stale `answered_at` → `[]`, `open` → `[]`, `closed` → as stored); `validate_files(value, answered_at: str) -> list[dict]` (raises `rules.RuleError` with a field path, e.g. `answer_files[2].bytes`); `download_name(ask_id: int, n: int, name: str) -> tuple[str, str]` (the ASCII `filename` and the RFC 5987 `filename*`); `ITEM_KEYS` (the 7.2 table's fields).

- [ ] **Step 1: Failing tests** — `test_clean_name_cases`: `"../x"→"x"`, `"a/b"→"a_b"`, bidi `"‮exe.txt"` has no U+202E, 300 chars → 200 with the extension kept, `"..."→"file"`, `"a\x00b"→"a_b"`, `"CON"→"_CON"`, `"nul.txt"→"_nul.txt"`, `"x. "→"x"`; `test_validate_files_fields`: each 7.2 field refused when wrong (`bytes` 0, `bytes` 104_857_601, secret with `bytes` 10_485_761, `secret` = `"true"`, bad `mime` → stored as `application/octet-stream`, `file_id` with `/`, `file_id` set with `deleted_at` set, `deleted_reason` outside the five values, 7 items, duplicate ids); `test_stale_and_open_read_as_empty`; `test_single_tenant_always_empty`; `test_download_name` (`ask-12-file-3.pdf` from `"Q3 report.PDF"`; no extension for `"x.toolongextension"`); Review Focus 5: `test_hand_edited_item_is_dropped_on_read` (7th item, bidi name, `file_id`+`deleted_at`).
- [ ] **Step 2:** Run `pytest server/tests/test_hosted_files.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement per 7.2 (NFC, the character classes and device names listed there; reuse the control/format/bidi stripping that client and agent names use).
- [ ] **Step 4:** Run → PASS; run `pytest server/tests -q` → no regressions.
- [ ] **Step 5:** Commit `Files in answers: answer_files validation and name cleaning`.

### Task 2: Answer writes, events, no-op rule, answer text

**Files:** Modify `server/rules.py` (answer: `files`, no-op rule, `previous_files`, reopen/close), `server/hosted/records.py` (PATCH by the app carries `answer_files`), `server/hosted/app_api.py` (event payloads); Test `server/tests/test_hosted_files.py`.

**Interfaces:**
- Consumes: `validate_files`, `clean_files` (Task 1).
- Produces: the ask field `answer_files` (always a list); event payloads `answered.files` / `answered.previous_files`, `reopened.answer_files`, new kind `file_downloaded {id}`; `answer_text_for(text, option, voice, files) -> str`.

- [ ] **Step 1: Failing tests** — `test_answer_with_files_in_one_write`; `test_noop_only_if_file_list_same` (same ids, `file_id`s, `secret` → no-op; one dropped or added → change); `test_kept_item_restamped` (same `id`/`file_id`/`secret`/`uploaded_at`, new `answered_at`); `test_answer_text_counts_files` (`"2 files attached"`, `"1 file attached"`, with voice: `"Voice answer (0:42), not transcribed · 2 files attached"`); `test_reopen_clears_files`; `test_no_agent_tool_or_rest_body_writes_answer_files`; `test_non_owner_sees_name_and_mime_null`; Review Focus 1 part: `test_reanswer_drops_item_and_reports_previous_files`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement as 7.2 / 7.3 (5, 7); event payloads as 7.2 «Events».
- [ ] **Step 4:** Run → PASS; full server suite green.
- [ ] **Step 5:** Commit.

### Task 3: What the owner agent reads — links, `for_reader(file_urls=…)`, webhook

**Files:** Modify `server/hosted/files.py` (`FileLinks`: seal/open with context `file`), `server/hosted/voice.py:155` `for_reader` (signature `for_reader(ask, links, reader, file_urls=True, file_links=None)`), `server/hosted/records.py:508`, `server/hosted/app_api.py:466` (webhook passes `file_urls=False`); Test `server/tests/test_hosted_files.py`.

**Interfaces:**
- Produces: `FileLinks(sealer, base_url)` with `url_for(user, agent, ask_id, answered_at, file_id, n) -> tuple[str, str]` (url, `url_expires_at`) and `open(token) -> dict | None` (claims exactly `{user, agent, ask_id, answered_at, file_id, n, exp}`, else None).

- [ ] **Step 1: Failing tests** — `test_owner_gets_url_for_normal_files_in_ask_get_and_asks_mine`; `test_asks_list_has_no_url`; `test_secret_file_has_no_url_anywhere` (ask_get, asks_mine, asks_list, REST, webhook); **`test_webhook_body_has_no_file_urls`** (no `url` on any file; a secret file is exactly `{id, secret: true}`); tokens: `test_file_token_context` (a voice token does not open as a file token and the reverse), tampered, expired, wrong agent, stale `answered_at` → `open` returns None.
- [ ] **Step 2–4:** FAIL → implement (separate opener; `voice.CLAIMS` untouched) → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 4: `DriveClient.stream_media` and the budgets

**Files:** Modify `server/drive.py` (new `stream_media(file_id: str, start: int = 0) -> Iterator[bytes]`, 1 MiB chunks, `Accept-Encoding: identity`, compressed body refused, token refresh as `_download_capped`; a `DriveBlocked` error for any 403 that is not in `RATE_LIMIT_REASONS`), `server/hosted/ratelimit.py` (`"files": (20, 60.0)`; a byte bucket `file_bytes` 1 GiB/3600 s per agent with `charge(key, n) -> bool`); Test `server/tests/test_drive.py`, `server/tests/test_hosted_files.py`, fake Drive gains `Range` and a streaming body.

- [ ] **Step 1: Failing tests** — `test_stream_media_chunks_and_range` (a 5 MiB fake body arrives in 1 MiB pieces; `start=N` sends `Range: bytes=N-`); `test_stream_media_peak_memory` (a 100 MiB fake body with tracemalloc peak under 8 MiB); `test_stream_media_403_abusive_is_blocked` and `test_stream_media_rate_limit_403_is_transient`; `test_file_bytes_bucket_charges_and_refuses` (1 GiB/h; refused charges nothing).
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 5: The normal-file route `GET /media/v1/<token>` (file tokens)

**Files:** Create `server/hosted/files_api.py` (`add_file_routes(app, links: FileLinks, accounts, pool, limiter, store)`), modify `server/hosted/app.py` (register; route by the token's context so voice tokens keep their route), `server/hosted/media_api.py` (`STREAM_SLOTS = 4`, separate from `MEDIA_SLOTS`); Test `server/tests/test_hosted_files_api.py`.

**Interfaces:**
- Consumes: `FileLinks.open` (Task 3), `stream_media`, `DriveBlocked`, buckets (Task 4), `download_name` (Task 1).
- Produces: `mark_downloaded(store, user, ask_id, n, file_id, answered_at, at) -> bool` (idempotent guards of 7.4; also used by Task 6).

- [ ] **Step 1: Failing tests** — the check order of 7.4 (one 404 message for the first group; closed or deleted → 410; reopened or stale → 404); headers exactly (Global Constraints), octet-stream even when Drive says `text/html`; Drive `size` ≠ `bytes` → 404; size over cap → 404; `Range: bytes=N-` → 206 with `Content-Range`; `HEAD` → 405; Drive unreachable → 503 `Retry-After: 30`; 403 abusive → 410 «Google blocked this file» and `blocked_at` set; rate-limit 403 → 503; slots: 5th concurrent → 503 `Retry-After: 5`; `files` bucket → 429; `downloaded_at` written once, after the response, only if the ask is still answered with this `file_id`/`answered_at`; Review Focus 3: `test_stream_longer_than_bytes_aborts_and_writes_nothing`; a short stream aborts; Review Focus 1: `test_dropped_file_mid_stream_ends_with_error_then_410` (fake Drive answers 404 mid-body after a re-answer drops the item; the next request with the old token is 410).
- [ ] **Step 2–4:** FAIL → implement (`StreamingResponse`, sent length checked against `bytes`, write in a `BackgroundTask`) → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 6: The secret route and the claim state machine

**Files:** Modify `server/hosted/files_api.py` (`GET /api/asks/{id}/files/{n}/content`, bearer = owner agent's OAuth token or agent key; the user's token → 403), `server/hosted/firestore_store.py` + `server/hosted/store.py` (collection `fileclaims`, transactional `claim_take`, `claim_release`, `claim_done`, `claim_delete_for_ask`), `server/hosted/accounts.py:186` (`delete_user` deletes the user's claims); Test `server/tests/test_hosted_files_api.py`.

**Interfaces:**
- Produces: `ClaimState = none | held(until) | done`; `claim_key(user, ask_id, answered_at, file_id) -> str` (a digest).

- [ ] **Step 1: Failing tests** — `test_secret_sent_once_then_410`; `test_held_claim_409`; `test_expired_held_claim_taken_over` (lease 960 s); `test_disconnect_mid_body_releases_claim`; `test_done_only_after_final_send_without_disconnect`; `test_delete_failure_leaves_done_and_route_refuses`; `test_secret_past_24h_is_410_expired`; `test_secret_no_range` (a `Range` header is ignored, 200 full body); `test_late_writes_with_other_answered_at_dropped`; `test_closed_ask_skipped`; `test_normal_file_through_bearer_route_streams`; Review Focus 2: `test_two_concurrent_requests_one_gets_bytes` (two threads on the fake store's transaction).
- [ ] **Step 2–4:** FAIL → implement as 7.4 «Secret file» (read whole, size-check before the first byte, one response; the `_Held` wrapper listens for `http.disconnect`; Drive delete + ask write after the response) → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 7: Close / reopen by the agent, Delete Account

**Files:** Modify the agent close/reopen path (where voice is deleted today: `server/rules.py:266` and its hosted caller), `server/hosted/accounts.py`; Test `server/tests/test_hosted_files.py`, `server/tests/test_hosted_delete_account.py`.

- [ ] **Step 1: Failing tests** — `test_agent_close_deletes_files_and_claims` (only files whose appProperties say `kind: "file"` and this `ask_id`; a foreign file is left; a Drive failure is logged by ids and the close stands; `deleted_reason: closed`); `test_agent_reopen_deletes_files` (`reopened`); `test_delete_user_removes_claims`.
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 8: The hourly sweep `POST /internal/sweep`

**Files:** Create `server/hosted/sweep.py` (`sweep(store, pool, now, cursor, limit) -> tuple[next_cursor, stats]`), modify `server/hosted/app.py` (route not in OpenAPI, no CORS, OIDC: `email` = the bridge's service account, audience `ASKS_PUBLIC_URL`, per Task 0's spike-3 ruling), `deploy/hosted/README.md` (the `gcloud scheduler jobs create http` line), `.github/workflows/deploy.yml` (`--timeout=900`); Test `server/tests/test_hosted_sweep.py`, `server/tests/test_deploy_config.py`.

- [ ] **Step 1: Failing tests** — OIDC accepted / wrong email / wrong audience / no token → 401 with an empty body; the four rules of 7.6 (secret 24 h unless held, done deleted regardless; closed asks' files; orphans at 1 h secret / 24 h normal by the name's time; normal files 30 days after `answered_at`); `deleted_at`/`deleted_reason` written with the guards; claims of closed asks deleted; slices: `limit` users per call, cursor resumes; Review Focus 4: `test_revoked_grant_user_skipped_and_cursor_advances`; `test_deploy_sets_timeout_900`.
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 9: Agent docs, policy, CLI

**Files:** Modify `server/mcp_tools.py` (docstrings of `asks_mine`, `ask_get`, `ask_close`, server instructions — the exact texts of 7.8), `server/hosted/pages.py` or the /agents page source (house rule, 7.8), `server/hosted/site.py` (privacy policy: 7.7 «Policy before the apps ship»), `cli/asks.py` (`askhooman files ID [--out DIR] [--secret]`: names `ask-<id>-file-<n>[.ext]`, size check, normal via `url`, secret via the bearer route); Test `server/tests/test_mcp.py`, `server/tests/test_cli.py`.

- [ ] **Step 1: Failing tests** — docstrings contain the 7.8 sentences verbatim; `test_cli_files_saves_with_fixed_names_and_checks_size`; `test_cli_files_secret_uses_bearer_route`; `test_cli_files_never_uses_name_as_path`.
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite.
- [ ] **Step 5:** Commit.

### Task 10: `ask_secret_file` — a small secret file inline over MCP

**Files:** Modify `server/mcp_tools.py` (hosted tool `ask_secret_file(id: int, n: int)`; docstring VERBATIM from SPEC 7.8), `server/hosted/files_api.py` (factor the secret send — claim take → read whole → size check → hand over → done (retried) → guarded Drive delete → `mark_sent` — into one function both the bearer route and the tool call), the asks_mine docstring text pinned by Task 9 (the new «with only an MCP connection…» clause), /llms.txt line; Test `server/tests/test_hosted_files_api.py`, `server/tests/test_mcp.py`.

- [ ] **Step 1: Failing tests** — `test_secret_file_inline_once` (returns {name, mime, bytes, content_base64}, exact bytes; second call «already sent»; the bearer route then 410; Drive delete called once); `test_secret_file_inline_over_1mib_refused_claim_untouched`; `test_secret_file_inline_normal_file_points_to_url`; `test_secret_file_inline_other_agent_404`; `test_secret_file_inline_held_claim_busy`; `test_secret_file_inline_no_bytes_in_logs` (markers a random token cannot contain); `test_secret_file_inline_files_bucket`; docstring and house-rule texts pinned verbatim; the tool exists in the hosted server only.
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite.
- [ ] **Step 5:** Commit.

### After the last task

Final whole-branch review; full `pytest server/tests -q`; PR; merge (triggers deploy: `server/**`); after deploy: the Scheduler job created with the README line; live check with a test ask (a 20 MiB normal file uploaded to the test user's appDataFolder by script, downloaded by the CLI, sizes compared; a secret 1 MiB file fetched twice → 200 then 410); the switch for the apps stays off until their plans ship.
