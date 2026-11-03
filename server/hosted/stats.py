"""The operator's view of the hosted bridge (SPEC "Operator stats"): who signed up and who uses it, counted from
what the store already keeps — users (email, sign-up time, the Google grant, id counters) and agents (client,
key, webhook, last seen). Nothing from anyone's Drive, no tokens or secrets in the result."""

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

WINDOWS = {"1d": 1, "7d": 7, "30d": 30}
KEY_CLIENT, UNKNOWN_CLIENT = "Agent key", "Unknown"


def internal_check(admins: Iterable[str], internal: Iterable[str]) -> Callable[[str | None], bool]:
    """Whether an email is one of the operator's own people: an admin, a listed email or in a listed "@domain"."""
    emails = {e.lower() for e in (*admins, *internal) if not e.startswith("@")}
    domains = tuple(e.lower() for e in internal if e.startswith("@"))

    def check(email: str | None) -> bool:
        email = (email or "").strip().lower()
        return bool(email) and (email in emails or email.endswith(domains))
    return check


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _within(moment: datetime | None, now: datetime) -> dict[str, bool]:
    return {name: moment is not None and now - moment <= timedelta(days=days) for name, days in WINDOWS.items()}


def _clients(agent: dict) -> list[str]:
    names = [c.get("client_name") for c in agent.get("clients") or [] if isinstance(c, dict)]
    names = [n for n in dict.fromkeys(names + [agent.get("client_name")]) if isinstance(n, str) and n]
    if agent.get("key_prefix"):
        names.append(KEY_CLIENT)
    return names or [UNKNOWN_CLIENT]


def _count(value) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _block() -> dict:
    return {"users": 0, "new": dict.fromkeys(WINDOWS, 0), "active": dict.fromkeys(WINDOWS, 0), "drive_linked": 0,
            "agents": 0, "agents_active": dict.fromkeys(WINDOWS, 0), "revoked_agents": 0, "webhooks": 0, "keys": 0,
            "clients": Counter(), "asks": 0, "tasks": 0}


def admin_stats(users: Iterable[dict], agents: Iterable[dict], now: datetime,
                internal: Callable[[str | None], bool]) -> dict:
    """Totals for all users and for external ones (not the operator's own people), and one row per account, newest
    first. A user is active in a window when one of their connected (not revoked) agents was seen in it; the
    asks and tasks counts are the user's id counters (the highest id issued). Agents of deleted users are left out."""
    rows: dict[str, dict] = {}
    for user in users:
        sub = user.get("sub")
        if not isinstance(sub, str):
            continue
        counters = user.get("counters")
        counters = counters if isinstance(counters, dict) else {}
        rows[sub] = {"email": user.get("email"), "internal": internal(user.get("email")),
                     "created_at": user.get("created_at"), "drive_linked": bool(user.get("google_refresh")),
                     "agents": 0, "asks": _count(counters.get("ask")), "tasks": _count(counters.get("task")),
                     "last_seen": None, "clients": []}
    every, external = _block(), _block()
    seen_at: dict[str, datetime] = {}
    for agent in agents:
        row = rows.get(agent.get("user") or "")
        if row is None:
            continue
        blocks = (every,) if row["internal"] else (every, external)
        if agent.get("revoked_at"):
            for block in blocks:
                block["revoked_agents"] += 1
            continue
        seen = _time(agent.get("last_seen"))
        clients = _clients(agent)
        row["agents"] += 1
        row["clients"] = sorted(set(row["clients"]) | set(clients))
        if seen and (agent["user"] not in seen_at or seen > seen_at[agent["user"]]):
            seen_at[agent["user"]] = seen
            row["last_seen"] = agent.get("last_seen")
        for block in blocks:
            block["agents"] += 1
            block["webhooks"] += bool(agent.get("webhook_url"))
            block["keys"] += bool(agent.get("key_prefix"))
            block["clients"].update(clients)
            for name, inside in _within(seen, now).items():
                block["agents_active"][name] += inside
    for sub, row in rows.items():
        blocks = (every,) if row["internal"] else (every, external)
        new, active = _within(_time(row["created_at"]), now), _within(seen_at.get(sub), now)
        for block in blocks:
            block["users"] += 1
            block["drive_linked"] += row["drive_linked"]
            block["asks"] += row["asks"]
            block["tasks"] += row["tasks"]
            for name in WINDOWS:
                block["new"][name] += new[name]
                block["active"][name] += active[name]
    for block in (every, external):
        block["clients"] = dict(block["clients"].most_common())
    accounts = sorted(rows.values(), key=lambda r: _time(r["created_at"]) or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
    return {"generated_at": _iso(now), "all": every, "external": external, "accounts": accounts}
