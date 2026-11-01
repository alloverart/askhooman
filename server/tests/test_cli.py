"""cli/asks.py task commands against the real server (stdlib urllib over localhost)."""

import importlib.util
import json

from .conftest import ALLEN, ASKS_DIR, DENIS

spec = importlib.util.spec_from_file_location("asks_cli", ASKS_DIR / "cli" / "asks.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(capsys, monkeypatch, base_url, token, *argv):
    monkeypatch.setenv("ASKS_TOKEN", token)
    code = cli.main(["--url", base_url, *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_task_commands(live_server, capsys, monkeypatch):
    def allen(*argv):
        return run(capsys, monkeypatch, live_server, ALLEN, *argv)

    code, out, _ = allen("task", "create", "--title", "Get the supplier to confirm the fabric", "--status", "waiting",
                         "--waiting-on", "Supplier support reply", "--next-check", "2000-01-01",
                         "--link", "Order|https://example.com/order", "--ref", "TG 5591")
    assert code == 0 and out.startswith("created task #1 [waiting] Get the supplier to confirm the fabric")
    code, out, _ = allen("tasks", "--mine")
    assert code == 0 and "#1" in out and "waiting on Supplier support reply · check 2000-01-01 (overdue)" in out
    assert "active 0 · waiting 1 · done 0 · dropped 0" in out
    code, out, _ = allen("task", "update", "1", "--status", "active", "--next-check", "", "--context", "line 1")
    assert code == 0 and out.startswith("updated task #1 [active]")
    code, out, _ = allen("task", "show", "1")
    assert code == 0 and "Waiting on: Supplier support reply" in out and "Next check" not in out
    assert "status: waiting -> active; context, next_check" in out and "Order — https://example.com/order" in out
    code, out, _ = allen("task", "done", "1", "--resolution", "Supplier confirmed")
    assert code == 0 and "=  Supplier confirmed" in out
    code, out, _ = allen("--json", "tasks", "--status", "finished")
    data = json.loads(out)
    assert [t["id"] for t in data["tasks"]] == [1] and data["counts"]["done"] == 1
    code, out, _ = allen("task", "reopen", "1", "--json")
    assert code == 0 and json.loads(out)["status"] == "active"

    code, _, err = run(capsys, monkeypatch, live_server, DENIS, "task", "drop", "1")
    assert code == 1 and err.startswith("error: HTTP 403:")
    code, _, err = allen("task", "update", "1")
    assert code == 1 and "nothing to update" in err
    code, _, err = allen("task", "create", "--title", "x", "--next-check", "Nov 3")
    assert code == 1 and "HTTP 422" in err and "next_check" in err
