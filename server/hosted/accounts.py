"""Users, agents, OAuth grants and agent keys of the hosted bridge, on top of the metadata Store.

An agent belongs to one user (a Google account) and is what every token resolves to: the asks/tasks it
writes carry its slug as `agent`. Tokens, codes, keys and client secrets are stored as SHA-256 digests only;
the user's Google refresh token and webhook secrets are encrypted (crypto.Keyring), bound to their owner.

Grants: an authorization code starts a token family; every refresh rotates the refresh token. Presenting a
used code or a used refresh token again revokes the whole family (OAuth 2.1 §4.3.1, refresh token reuse): a
families/{id} document marks it, and every use and every issuance checks that mark, so tokens written by a
concurrent request after the sweep die too. Revoking an agent kills all its tokens and keys at once (they are
checked against the agent on every use).

Agents: a name is turned into a slug that owns the asks. A client may connect as an existing agent only if it
did so before, or if the user explicitly confirmed it on the consent page (NameTaken otherwise), so a new
client can't take over an agent just by calling itself by the agent's name. Each agent keeps the list of
clients that connected as it.
"""

import hashlib
import logging
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass

from ..db import utc_now
from ..rules import _text
from ..tokens import AGENT_SLUG, Identity
from .clients import DCR_ID, clean_label
from .config import HostedSettings
from .crypto import Keyring, digest, new_secret, same
from .store import DELETE, UNCHANGED, Store

log = logging.getLogger("asks.hosted.accounts")

AGENT_NAME_MAX = 40
SEEN_EVERY = 300.0  # write an agent's last_seen at most this often (seconds)
SCOPE = "qtracker"  # OAuth scope from before the rename to AskHooman: clients and tokens carry it, kept as is
ACCESS_PREFIX, REFRESH_PREFIX, KEY_PREFIX = "qta_", "qtr_", "qtk_"
ACCESS_FORMAT = re.compile(r"^qta_[A-Za-z0-9_-]{43}$")  # new_secret(ACCESS_PREFIX): 32 random bytes
KEY_FORMAT = re.compile(r"^qtk_[A-Za-z0-9_-]{40}$")  # new_secret(KEY_PREFIX, 30)
AGENT_ID = re.compile(r"^ag_[A-Za-z0-9_-]{16}$")
RESERVED_SLUGS = ("denis",)  # "denis" is the human's actor name in Drive files (closed_by, events)
CLIENTS_PER_AGENT = 20
UNUSED_CLIENT_TTL = 86400  # a dynamic registration nobody signs in with is forgotten after a day
CLIENT_TTL = 180 * 86400  # one that is used lives this long after its last sign-in/refresh

_CYRILLIC = dict(zip(  # noqa: B905 (both 38 long)
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґў",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
     "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g", "u"],
))


class NameTaken(Exception):
    """The agent name belongs to another active agent that this client hasn't connected as before."""

    def __init__(self, agent: dict):
        super().__init__(f"an agent named {agent['name']} is already connected")
        self.agent = agent


class OAuthError(Exception):
    """An OAuth error response (RFC 6749 §5.2 codes)."""

    def __init__(self, code: str, description: str, status: int = 400):
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status


@dataclass(frozen=True)
class HostedIdentity(Identity):
    """An agent of one user: role "agent", `agent` = its slug (the asks' owner), plus whose Drive it uses."""
    user: str = ""
    agent_id: str = ""
    client_id: str | None = None
    via: str = "oauth"  # or "key"


def slugify(name: str) -> str:
    """Agent name -> the `agent` slug written on its asks and tasks ("Research Bot" -> "research-bot"). Names with
    nothing left in ASCII get a stable suffix from the name ("日本語" -> "agent-1a2b3c"), so different names stay
    different agents; reserved slugs ("denis") get "-agent"."""
    lowered = clean_label(name).lower()
    text = "".join(_CYRILLIC.get(ch, ch) for ch in lowered)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:32].strip("-")
    if not AGENT_SLUG.match(text or ""):
        return "agent-" + hashlib.sha256(lowered.encode("utf-8")).hexdigest()[:6]
    if text in RESERVED_SLUGS:
        return f"{text}-agent"
    return text


def agent_json(agent: dict) -> dict:
    """An agent as the App API returns it (no secrets)."""
    return {
        "id": agent["id"], "name": agent["name"], "slug": agent["slug"],
        "client_name": agent.get("client_name"), "client_id": agent.get("client_id"),
        "has_key": bool(agent.get("key_prefix")), "key_prefix": agent.get("key_prefix"),
        "created_at": agent.get("created_at"), "last_seen": agent.get("last_seen"),
        "webhook_url": agent.get("webhook_url"), "webhook_secret_set": bool(agent.get("webhook_secret")),
        "revoked_at": agent.get("revoked_at"),
        "clients": [{"client_id": c.get("client_id"), "client_name": c.get("client_name"),
                     "connected_at": c.get("connected_at")} for c in agent.get("clients") or []],
    }


class Accounts:
    def __init__(self, store: Store, keyring: Keyring, settings: HostedSettings, clock=time.time):
        self.store = store
        self.keyring = keyring
        self.settings = settings
        self.clock = clock
        self._seen: dict[str, float] = {}
        self._seen_lock = threading.Lock()

    # ---------- users ----------

    def user(self, sub: str) -> dict | None:
        return self.store.get("users", sub)

    def touch_user(self, sub: str, email: str | None) -> dict:
        """Make sure the user exists (first sign-in through the app or the bridge); keep the email current."""
        def change(doc):
            if doc is None:
                return {"sub": sub, "email": email, "created_at": utc_now(), "counters": {}}, None
            if email and doc.get("email") != email:
                doc["email"] = email
                return doc, None
            return UNCHANGED, None
        self.store.transact("users", sub, change)
        return self.user(sub)

    def link_google(self, sub: str, email: str | None, refresh_token: str) -> None:
        sealed = self.keyring.encrypt(refresh_token, f"google-refresh:{sub}")

        def change(doc):
            doc = doc or {"sub": sub, "created_at": utc_now(), "counters": {}}
            doc.update(google_refresh=sealed, google_linked_at=utc_now())
            if email:
                doc["email"] = email
            return doc, None
        self.store.transact("users", sub, change)

    def google_refresh(self, sub: str) -> str | None:
        sealed = (self.user(sub) or {}).get("google_refresh")
        return self.keyring.decrypt(sealed, f"google-refresh:{sub}") if sealed else None

    def drive_linked(self, sub: str) -> bool:
        return bool((self.user(sub) or {}).get("google_refresh"))

    def unlink_google(self, sub: str) -> None:
        """Google said the refresh token is dead (revoked by the user): forget it, so the app can ask to relink."""
        self.store.update("users", sub, {"google_refresh": None})

    def delete_user(self, sub: str) -> bool:
        """Delete Account: forget the user and everything kept for them here: their agents (names, clients,
        webhooks and encrypted webhook secrets), OAuth access and refresh tokens, unredeemed authorization codes,
        agent keys, pending sign-ins, and last the user document (email, encrypted Google refresh token, id
        counters). False if there was nothing to delete. The asks and tasks in their Drive are not touched: they
        are the user's files. Revoking the Google grant is the caller's job (read google_refresh first).

        The token families are marked revoked before anything is deleted and the agents go first, so a code
        exchange or refresh racing with this fails; the user document goes last, so a retry after a failure
        halfway still finds the account. Registrations of OAuth clients (clients/…) belong to no user and stay."""
        found = self.user(sub) is not None
        grants = self.store.find("tokens", "user", sub) + self.store.find("codes", "user", sub)
        families = sorted({doc["family"] for _, doc in grants if doc.get("family")})
        for family in families:
            self.store.put("families", family, {"revoked": True, "revoked_at": utc_now(),
                                                "expires_at": self.clock() + self.settings.refresh_ttl})
        removed: dict[str, int] = {}
        for collection in ("agents", "tokens", "codes", "keys", "requests"):
            for key, _ in self.store.find(collection, "user", sub):
                self.store.delete(collection, key)
                removed[collection] = removed.get(collection, 0) + 1
                if collection == "agents":
                    with self._seen_lock:
                        self._seen.pop(key, None)
        self.store.delete("users", sub)
        if found or removed:
            log.info("account deleted: %s", ", ".join(f"{n} {c}" for c, n in removed.items()) or "no agents")
        return found or bool(removed)

    # ---------- per-user id counters (new ask/task ids) ----------

    def has_counter(self, sub: str, kind: str) -> bool:
        return kind in ((self.user(sub) or {}).get("counters") or {})

    def raise_counter(self, sub: str, kind: str, at_least: int) -> None:
        """Counter = max(counter, at_least): the first use (from the highest id in Drive), or after a collision."""
        def change(doc):
            doc = doc or {"sub": sub, "created_at": utc_now(), "counters": {}}
            counters = doc.setdefault("counters", {})
            if counters.get(kind, -1) >= at_least:
                return UNCHANGED, None
            counters[kind] = at_least
            return doc, None
        self.store.transact("users", sub, change)

    def next_id(self, sub: str, kind: str) -> int:
        def change(doc):
            doc = doc or {"sub": sub, "created_at": utc_now(), "counters": {}}
            counters = doc.setdefault("counters", {})
            counters[kind] = int(counters.get(kind, 0)) + 1
            return doc, counters[kind]
        return self.store.transact("users", sub, change)

    # ---------- agents ----------

    def agent(self, agent_id: str) -> dict | None:
        return self.store.get("agents", agent_id) if isinstance(agent_id, str) and AGENT_ID.match(agent_id) else None

    def agents(self, sub: str, include_revoked: bool = False) -> list[dict]:
        found = [doc for _, doc in self.store.find("agents", "user", sub)]
        if not include_revoked:
            found = [a for a in found if not a.get("revoked_at")]
        return sorted(found, key=lambda a: (a.get("created_at") or "", a["id"]))

    def own_agent(self, sub: str, agent_id: str) -> dict | None:
        agent = self.agent(agent_id)
        if agent is None or agent.get("user") != sub or agent.get("revoked_at"):
            return None
        return agent

    @staticmethod
    def client_list(agent: dict) -> list[dict]:
        return [c for c in agent.get("clients") or [] if isinstance(c, dict)]

    def client_ids(self, agent: dict) -> set[str]:
        return {c.get("client_id") for c in self.client_list(agent) if c.get("client_id")}

    def connect_agent(self, sub: str, name, client_id: str | None, client_name: str | None,
                      confirm: str | None = None, dry_run: bool = False, shared_client: bool = False) -> dict:
        """The agent a consent (client_id set) or a new key (client_id None) connects as.

        A name whose slug belongs to one of the user's active agents connects as that agent only if this client
        connected as it before, or `confirm` equals that slug (the user said "connect as the existing agent");
        otherwise NameTaken. shared_client: a CIMD client_id is the same for every install of the app (every Claude
        Code), so "connected before" proves nothing and only `confirm` counts. The agent's original
        client_id/client_name never change; connecting clients are added to its `clients` list. dry_run: only check
        (returns the existing agent or {})."""
        name = clean_agent_name(name)
        slug = slugify(name)
        now = utc_now()
        for agent in self.agents(sub):
            if agent["slug"] != slug:
                continue
            known = client_id is not None and client_id in self.client_ids(agent)
            if not (known and not shared_client) and confirm != slug:
                raise NameTaken(agent)
            if dry_run:
                return agent
            fields: dict = {"last_seen": now}
            if client_id and not known:
                clients = self.client_list(agent) + [{"client_id": client_id, "client_name": client_name,
                                                      "connected_at": now}]
                fields["clients"] = clients[-CLIENTS_PER_AGENT:]
                log.info("client %s connected as existing agent %s (confirmed by the user)", client_id, agent["id"])
            self.store.update("agents", agent["id"], fields)
            return {**agent, **fields}
        if dry_run:
            return {}
        clients = [{"client_id": client_id, "client_name": client_name, "connected_at": now}] if client_id else []
        agent = {"id": "ag_" + new_secret(nbytes=12), "user": sub, "name": name, "slug": slug,
                 "client_id": client_id, "client_name": client_name, "clients": clients, "created_at": now,
                 "last_seen": None, "webhook_url": None, "webhook_secret": None, "revoked_at": None,
                 "key_prefix": None}
        self.store.put("agents", agent["id"], agent)
        log.info("agent %s (%s) connected for a user via %s", agent["id"], slug, "oauth" if client_id else "key")
        return agent

    def rename_agent(self, agent: dict, name) -> dict:
        """Only the display name changes: the slug stays, so the agent keeps owning its asks and tasks."""
        name = clean_agent_name(name)
        self.store.update("agents", agent["id"], {"name": name})
        return {**agent, "name": name}

    def set_webhook(self, agent: dict, url: str | None, rotate: bool = False) -> tuple[dict, str | None]:
        """Set/clear the webhook URL. A new signing secret is made (and returned, once) when the agent gets a
        webhook and has none yet, or on rotate."""
        fields: dict = {"webhook_url": url}
        secret = None
        if url is None:
            fields["webhook_secret"] = None
        elif rotate or not agent.get("webhook_secret"):
            secret = "whsec_" + new_secret(nbytes=32)
            fields["webhook_secret"] = self.keyring.encrypt(secret, f"webhook:{agent['id']}")
        self.store.update("agents", agent["id"], fields)
        return {**agent, **fields}, secret

    def webhook_secret(self, agent: dict) -> str | None:
        sealed = agent.get("webhook_secret")
        return self.keyring.decrypt(sealed, f"webhook:{agent['id']}") if sealed else None

    def revoke_agent(self, agent: dict) -> dict:
        now = utc_now()
        self.store.update("agents", agent["id"], {"revoked_at": now, "webhook_url": None, "webhook_secret": None})
        for collection in ("tokens", "keys"):  # tidy up; the agent check alone already rejects them
            for key, _ in self.store.find(collection, "agent_id", agent["id"]):
                self.store.update(collection, key, {"revoked": True})
        log.info("agent %s revoked", agent["id"])
        return {**agent, "revoked_at": now, "webhook_url": None, "webhook_secret": None}

    def seen(self, agent_id: str) -> None:
        now = time.monotonic()
        with self._seen_lock:
            if now - self._seen.get(agent_id, -SEEN_EVERY) < SEEN_EVERY:
                return
            self._seen[agent_id] = now
        self.store.update("agents", agent_id, {"last_seen": utc_now()})

    # ---------- dynamically registered clients ----------

    def register_client(self, client: dict) -> tuple[dict, str | None]:
        """A new dynamic registration. Unused ones expire after a day (Firestore TTL on expire_time, and checked
        here); each sign-in or refresh extends a used one (client_used)."""
        client = dict(client, client_id="qtc_" + new_secret(nbytes=16), client_id_issued_at=int(self.clock()),
                      expires_at=self.clock() + UNUSED_CLIENT_TTL)
        secret = None
        if client["token_endpoint_auth_method"] != "none":
            secret = new_secret(nbytes=32)
            client["secret_hash"] = digest(secret)
        self.store.put("clients", client["client_id"], client)
        return client, secret

    def dcr_client(self, client_id: str) -> dict | None:
        if not isinstance(client_id, str) or not DCR_ID.match(client_id):  # also keeps "/" out of document keys
            return None
        client = self.store.get("clients", client_id)
        if client is None or client.get("expires_at", float("inf")) < self.clock():
            return None
        return client

    def client_used(self, client_id: str) -> None:
        """A registration that signs in / refreshes stays; write at most when its expiry is half used up."""
        client = self.dcr_client(client_id)
        if client is not None and client.get("expires_at", 0) < self.clock() + CLIENT_TTL / 2:
            self.store.update("clients", client_id, {"expires_at": self.clock() + CLIENT_TTL})

    @staticmethod
    def client_secret_ok(client: dict, secret: str | None) -> bool:
        expected = client.get("secret_hash")
        if not expected:
            return True  # public client
        return bool(secret) and same(digest(secret), expected)

    # ---------- pending authorizations ----------

    def start_request(self, data: dict) -> str:
        rid = new_secret(nbytes=32)
        self.store.put("requests", digest(rid), {**data, "expires_at": self.clock() + self.settings.request_ttl})
        return rid

    def request(self, rid: str | None) -> dict | None:
        if not rid:
            return None
        doc = self.store.get("requests", digest(rid))
        if doc is None or doc.get("expires_at", 0) < self.clock():
            return None
        return doc

    def update_request(self, rid: str, fields: dict) -> None:
        self.store.update("requests", digest(rid), fields)

    def finish_request(self, rid: str) -> dict | None:
        """Take the pending authorization (once: a second consent POST finds nothing)."""
        def take(doc):
            return (DELETE, doc) if doc is not None else (UNCHANGED, None)
        doc = self.store.transact("requests", digest(rid), take)
        if doc is None or doc.get("expires_at", 0) < self.clock():
            return None
        return doc

    # ---------- authorization codes ----------

    def issue_code(self, data: dict) -> str:
        code = new_secret(nbytes=32)
        self.store.put("codes", digest(code), {**data, "family": uuid.uuid4().hex, "used": False,
                                               "expires_at": self.clock() + self.settings.code_ttl})
        return code

    def redeem_code(self, code: str, client_id: str) -> dict:
        """The code's grant, marking it used. A code used twice revokes everything issued from it."""
        def use(doc):
            if doc is None or doc.get("client_id") != client_id:
                return UNCHANGED, ("missing", None)
            if doc.get("used"):
                return UNCHANGED, ("reused", doc)
            if doc.get("expires_at", 0) < self.clock():
                return UNCHANGED, ("expired", None)
            return {**doc, "used": True}, ("ok", doc)
        outcome, doc = self.store.transact("codes", digest(code), use)
        if outcome == "reused":
            log.warning("authorization code used twice: revoking its tokens")
            self.revoke_family(doc["family"])
        if outcome != "ok" or self.family_revoked(doc["family"]):
            raise OAuthError("invalid_grant", "authorization code is invalid, expired or already used")
        return doc

    # ---------- tokens ----------

    def family_revoked(self, family: str | None) -> bool:
        return bool(family) and bool((self.store.get("families", family) or {}).get("revoked"))

    def issue_tokens(self, grant: dict, family: str) -> dict:
        """A new access token + rotating refresh token for a grant {user, agent_id, client_id, resource, scope}.
        The family is checked after the tokens are written: a revocation that raced with this issuance wins."""
        now = self.clock()
        base = {key: grant[key] for key in ("user", "agent_id", "client_id", "resource", "scope")}
        access, refresh = new_secret(ACCESS_PREFIX), new_secret(REFRESH_PREFIX)
        self.store.put("tokens", digest(access), {**base, "kind": "access", "family": family, "revoked": False,
                                                  "created_at": now, "expires_at": now + self.settings.access_ttl})
        self.store.put("tokens", digest(refresh), {**base, "kind": "refresh", "family": family, "revoked": False,
                                                   "used": False, "created_at": now,
                                                   "expires_at": now + self.settings.refresh_ttl})
        if self.family_revoked(family):
            raise OAuthError("invalid_grant", "this grant was revoked")
        self.seen(grant["agent_id"])
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.settings.access_ttl,
                "refresh_token": refresh, "scope": grant["scope"]}

    def use_refresh(self, token: str, client_id: str) -> dict:
        """The grant of a refresh token, marking it used (rotation). Reuse of a used one revokes its family."""
        def use(doc):
            if doc is None or doc.get("kind") != "refresh" or doc.get("client_id") != client_id:
                return UNCHANGED, ("missing", None)
            if doc.get("revoked"):
                return UNCHANGED, ("revoked", None)
            if doc.get("used"):
                return UNCHANGED, ("reused", doc)
            if doc.get("expires_at", 0) < self.clock():
                return UNCHANGED, ("expired", None)
            return {**doc, "used": True, "used_at": self.clock()}, ("ok", doc)
        outcome, doc = self.store.transact("tokens", digest(token), use)
        if outcome == "reused":
            log.warning("refresh token reused: revoking its token family (agent %s)", doc.get("agent_id"))
            self.revoke_family(doc["family"])
        if outcome != "ok" or self.family_revoked(doc["family"]):
            raise OAuthError("invalid_grant", "refresh token is invalid, expired, revoked or already used")
        if self.own_agent(doc["user"], doc["agent_id"]) is None:
            raise OAuthError("invalid_grant", "the agent was disconnected")
        return doc

    def revoke_family(self, family: str) -> None:
        """Mark the family first (every check reads the mark), then tidy up its tokens."""
        self.store.put("families", family, {"revoked": True, "revoked_at": utc_now(),
                                            "expires_at": self.clock() + self.settings.refresh_ttl})
        for key, _ in self.store.find("tokens", "family", family):
            self.store.update("tokens", key, {"revoked": True})

    def revoke(self, token: str, client_id: str) -> None:
        """RFC 7009: revoke an access or refresh token of this client, and with it its whole grant."""
        doc = self.store.get("tokens", digest(token))
        if doc is not None and doc.get("client_id") == client_id:
            self.revoke_family(doc["family"])

    # ---------- agent keys ----------

    def create_key(self, sub: str, name, existing: bool = False) -> tuple[dict, str]:
        """A new agent key. A name of an existing agent adds the key to it only with existing=True (NameTaken)."""
        agent = self.connect_agent(sub, name, None, None, confirm=slugify(clean_agent_name(name)) if existing else None)
        key = new_secret(KEY_PREFIX, nbytes=30)
        self.store.put("keys", digest(key), {"user": sub, "agent_id": agent["id"], "created_at": utc_now(),
                                              "revoked": False})
        prefix = key[:12]
        self.store.update("agents", agent["id"], {"key_prefix": prefix})
        return {**agent, "key_prefix": prefix}, key

    # ---------- the resource server's view ----------

    def identify(self, bearer: str, resource: str) -> HostedIdentity | None:
        """The agent an MCP/REST bearer token (OAuth access token or agent key) belongs to, or None.
        OAuth tokens must have been issued for `resource` (audience binding)."""
        if not well_formed(bearer):
            return None
        if bearer.startswith(KEY_PREFIX):
            doc, via = self.store.get("keys", digest(bearer)), "key"
        else:
            doc, via = self.store.get("tokens", digest(bearer)), "oauth"
            if doc is not None and (doc.get("kind") != "access" or doc.get("expires_at", 0) < self.clock()
                                    or doc.get("resource") != resource):
                doc = None
        if doc is None or doc.get("revoked") or (via == "oauth" and self.family_revoked(doc.get("family"))):
            return None
        agent = self.own_agent(doc["user"], doc["agent_id"])
        if agent is None:
            return None
        self.seen(agent["id"])
        return HostedIdentity("agent", agent["slug"], user=doc["user"], agent_id=agent["id"],
                              client_id=doc.get("client_id"), via=via)


def clean_agent_name(name) -> str:
    """The agent's display name (Invalid -> 422 / shown on the consent page): one line, no control, format or
    bidi characters."""
    return _text(clean_label(name) if isinstance(name, str) else name, "agent name", AGENT_NAME_MAX, required=True)


def well_formed(bearer: str) -> bool:
    """Our own token formats only: anything else is rejected without a store lookup."""
    return bool(ACCESS_FORMAT.match(bearer) or KEY_FORMAT.match(bearer))
