"""Hosted bridge: Delete Account (DELETE /api/me on the App API)."""

from .hosted_support import (
    DENIS_SUB,
    LOOPBACK,
    OLGA_EMAIL,
    OLGA_SUB,
    RESOURCE,
    authorize,
    connect,
    consent,
    id_token,
    mcp,
    pkce,
    query,
    register,
    to_consent,
    tool_result,
)

USER_COLLECTIONS = ("agents", "tokens", "codes", "keys", "requests")


def held_for(store, sub: str) -> dict[str, int]:
    """How many documents of each per-user collection belong to `sub` (+ the user document itself)."""
    held = {c: sum(1 for doc in store.all(c).values() if doc.get("user") == sub) for c in USER_COLLECTIONS}
    held["users"] = int(sub in store.all("users"))
    return held


def test_delete_account_forgets_everything_and_revokes_google(hosted, google):
    store = hosted.store
    # Denis: an OAuth agent with a webhook, an agent key, an ask in his Drive, an authorization code nobody redeemed
    # yet and a sign-in waiting on the consent page
    client_id = register(hosted)["client_id"]
    denis = connect(hosted, google, client_id, agent="Allen")
    allen_id = hosted.get("/api/agents", headers=id_token(google)).json()["agents"][0]["id"]
    hosted.patch(f"/api/agents/{allen_id}", json={"webhook_url": "https://hooks.example/a"}, headers=id_token(google))
    key = hosted.post("/api/agents/keys", json={"name": "Hamilton"}, headers=id_token(google)).json()["key"]
    ask = tool_result(mcp(hosted, denis["access_token"], "ask_create", {"title": "Ship?"}))
    verifier, challenge = pkce()
    issued = consent(hosted, to_consent(hosted, google, authorize(hosted, client_id, challenge)), "Allen")
    code = query(issued.headers["location"])["code"]
    to_consent(hosted, google, authorize(hosted, register(hosted)["client_id"], pkce()[1]))
    # Olga, another user
    olga = connect(hosted, google, register(hosted)["client_id"], agent="Ogilvy", sub=OLGA_SUB, email=OLGA_EMAIL)
    olga_before = held_for(store, OLGA_SUB)
    assert all(held_for(store, DENIS_SUB).values()), held_for(store, DENIS_SUB)
    denis_grants = {t for t, sub in google.refresh_tokens.items() if sub == DENIS_SUB}
    assert denis_grants

    # only the app can delete an account: not without a token, not with an agent's token or key
    assert hosted.delete("/api/me").status_code == 401
    for bearer in (denis["access_token"], key):
        assert hosted.delete("/api/me", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401
    assert all(held_for(store, DENIS_SUB).values())

    deleted = hosted.delete("/api/me", headers=id_token(google))
    assert deleted.status_code == 204 and deleted.content == b""

    # nothing of his is left in the store, and Google's grant to the bridge is revoked
    assert held_for(store, DENIS_SUB) == {c: 0 for c in (*USER_COLLECTIONS, "users")}
    assert denis_grants <= set(google.revocations) and denis_grants <= google.revoked_refresh
    assert DENIS_SUB not in hosted.app.state.pool._clients  # no cached Drive access token either
    # every credential of his agents is dead
    assert mcp(hosted, denis["access_token"], "asks_mine").status_code == 401
    assert mcp(hosted, key, "asks_mine").status_code == 401
    assert hosted.get("/api/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    refreshed = hosted.post("/token", data={"grant_type": "refresh_token", "refresh_token": denis["refresh_token"],
                                            "client_id": client_id})
    assert refreshed.json()["error"] == "invalid_grant"
    redeemed = hosted.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                           "redirect_uri": LOOPBACK, "code_verifier": verifier, "resource": RESOURCE})
    assert redeemed.json()["error"] == "invalid_grant"
    # his asks and tasks are his files: they stay in his Drive
    assert google.drive(DENIS_SUB).doc(ask["id"])["title"] == "Ship?"

    # Olga is untouched
    assert held_for(store, OLGA_SUB) == olga_before
    assert not {t for t, sub in google.refresh_tokens.items() if sub == OLGA_SUB} & set(google.revocations)
    assert tool_result(mcp(hosted, olga["access_token"], "ask_create", {"title": "Still here?"}))["agent"] == "ogilvy"
    assert [a["name"] for a in hosted.get("/api/agents", headers=id_token(google, sub=OLGA_SUB)).json()["agents"]] \
        == ["Ogilvy"]

    # again: nothing to delete (and the call doesn't create an account to delete)
    again = hosted.delete("/api/me", headers=id_token(google))
    assert again.status_code == 404 and again.json()["detail"]
    assert DENIS_SUB not in store.all("users")
    # signing in to the app again starts a new, empty account
    assert hosted.get("/api/agents", headers=id_token(google)).json() == {"agents": []}
    assert hosted.get("/api/connect-info", headers=id_token(google)).json()["drive_linked"] is False


def test_delete_account_when_google_does_not_answer(hosted, google):
    tokens = connect(hosted, google, register(hosted)["client_id"])
    google.revoke_status = 503  # revocation is best effort: the account is deleted anyway
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert len(google.revocations) == 1
    assert held_for(hosted.store, DENIS_SUB) == {c: 0 for c in (*USER_COLLECTIONS, "users")}
    assert mcp(hosted, tokens["access_token"], "asks_mine").status_code == 401


def test_delete_account_of_an_app_only_user(hosted, google):
    """Signed in to the app, never linked Drive at the bridge: nothing to revoke, the account still goes."""
    assert hosted.get("/api/agents", headers=id_token(google)).status_code == 200  # creates the user
    assert hosted.post("/api/agents/keys", json={"name": "Bot"}, headers=id_token(google)).status_code == 201
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert google.revocations == []
    assert held_for(hosted.store, DENIS_SUB) == {c: 0 for c in (*USER_COLLECTIONS, "users")}
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 404


def test_delete_user_removes_claims(hosted, google):
    """The claims of a secret file (SPEC 7.4) carry their user: Delete Account removes them with the rest."""
    store = hosted.store
    connect(hosted, google, register(hosted)["client_id"])
    store.put("fileclaims", "c1", {"user": DENIS_SUB, "ask_id": 1, "state": "done"})
    store.put("fileclaims", "c2", {"user": OLGA_SUB, "ask_id": 1, "state": "held"})
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert set(store.all("fileclaims")) == {"c2"}


# ---------- fix round 2: M7 (the answer files go from the user's Drive before the grant does) ----------

def put_answer_file(drive, ask_id, n, secret=False, **props):
    marks = {"ask_id": str(ask_id), "kind": "file", "file": str(n), "secret": "1" if secret else "0", **props}
    return drive.add(f"afile-{ask_id}-{n}-1700000000000", b"Q3 report", marks, mime="application/octet-stream")


def test_delete_account_deletes_the_answer_files_in_the_users_drive_first(hosted, google):
    connect(hosted, google, register(hosted)["client_id"])
    drive = google.drive(DENIS_SUB)
    mine = [put_answer_file(drive, 1, 1), put_answer_file(drive, 1, 2, secret=True), put_answer_file(drive, 4, 1)]
    others = [
        drive.add("afile-9-1-1700000000000", b"x", {}),  # no marks: not ours
        drive.add("afile-9-2-1700000000000", b"x", {"kind": "voice", "ask_id": "9", "file": "2"}),
        drive.add("afile-9-3-1700000000000", b"x", {"kind": "file", "ask_id": "8", "file": "3"}),  # another ask's marks
        drive.add("ask-1.json", {"id": 1}, {"ask_id": "1"}),
    ]
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert not any(file_id in drive.files for file_id in mine)
    assert all(file_id in drive.files for file_id in others)  # the asks and everything not marked stay
    assert len(google.revocations) == 1  # and the grant went after the files


def test_delete_account_goes_on_when_drive_does_not_answer(hosted, google, caplog):
    connect(hosted, google, register(hosted)["client_id"])
    drive = google.drive(DENIS_SUB)
    kept = put_answer_file(drive, 1, 1)
    drive.down = True
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    drive.down = False
    assert kept in drive.files  # the user can remove the app data in Google's settings (the policy says so)
    assert held_for(hosted.store, DENIS_SUB) == {c: 0 for c in (*USER_COLLECTIONS, "users")}
    assert "Q3 report" not in caplog.text and "afile-" not in caplog.text


def test_delete_account_counts_and_logs_the_files_left_past_the_cap(hosted, google, monkeypatch, caplog):
    import logging
    from server.hosted import sweep as sweep_module
    connect(hosted, google, register(hosted)["client_id"])
    drive = google.drive(DENIS_SUB)
    files = [put_answer_file(drive, 1, n) for n in range(1, 6)]
    monkeypatch.setattr(sweep_module, "PURGE_LIMIT", 2)
    caplog.set_level(logging.INFO)
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert sum(f in drive.files for f in files) == 3
    assert "2 answer files deleted from Drive, 0 not, 3 left past the cap" in caplog.text
    assert "afile-" not in caplog.text and "Q3 report" not in caplog.text


def test_purge_says_how_many_it_left():
    from server.hosted.sweep import purge_answer_files
    class NoDrive:
        def records(self, user):
            raise RuntimeError("no grant")
    assert purge_answer_files(NoDrive(), "u") == (0, 1, 0, False)


def test_delete_account_says_at_least_when_the_listing_was_cut(hosted, google, monkeypatch, caplog):
    import logging
    from server.hosted import sweep as sweep_module
    connect(hosted, google, register(hosted)["client_id"])
    drive = google.drive(DENIS_SUB)
    for n in range(1, 6):
        put_answer_file(drive, 1, n)
    hosted.app.state.pool.client(DENIS_SUB).max_files = 3  # Drive has five, we see three
    monkeypatch.setattr(sweep_module, "PURGE_LIMIT", 2)
    caplog.set_level(logging.INFO)
    assert hosted.delete("/api/me", headers=id_token(google)).status_code == 204
    assert "2 answer files deleted from Drive, 0 not, at least 2 left past the cap" in caplog.text
