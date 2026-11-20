"""Cloud Run settings the code depends on. Agents waiting for their user hold a request for up to 50 s (GET
/api/agent/events, ask_wait, the Claude Code plugin, `asks.py listen`), essentially all the time. With Cloud Run's
default of 80 concurrent requests per instance and at most 3 instances, about 240 listening agents would take every
slot and stall the apps (review 06.11.2026). Held polls use no CPU, so the service allows 1000 per instance; the
deploy sets it on every rollout, and the guide's first deploy does too."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONCURRENCY = 1000


def test_every_deploy_sets_the_concurrency_for_long_polls():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    deploy = workflow[workflow.index("gcloud run deploy"):]
    deploy = deploy[:deploy.index("--quiet")]
    assert re.search(rf"--concurrency[ =]{CONCURRENCY}\b", deploy), deploy


def test_the_guide_sets_it_and_says_why():
    guide = (ROOT / "deploy" / "hosted" / "README.md").read_text()
    assert f"--concurrency={CONCURRENCY}" in guide
    assert "`--concurrency=1000`" in guide  # explained under "Why these flags"


def test_deploy_sets_timeout_900():
    """A file download may take REQUEST_TIMEOUT = 900 s and the hourly sweep runs slices for up to 10 minutes in one
    request: Cloud Run's default of 300 s would cut both."""
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    deploy = workflow[workflow.index("gcloud run deploy"):]
    deploy = deploy[:deploy.index("--quiet")]
    assert re.search(r"--timeout[ =]900\b", deploy), deploy
    guide = (ROOT / "deploy" / "hosted" / "README.md").read_text()
    assert "--timeout=900" in guide


def test_the_guide_creates_the_hourly_sweep_job_with_an_oidc_token():
    guide = (ROOT / "deploy" / "hosted" / "README.md").read_text()
    job = guide[guide.index("gcloud scheduler jobs create http"):]
    job = job[:job.index("```")]
    assert "--schedule=\"0 * * * *\"" in job and "/internal/sweep" in job and "--http-method=POST" in job
    assert "--oidc-service-account-email=$SA" in job and "--oidc-token-audience=https://<PUBLIC_HOST>" in job
    assert "ASKS_SWEEP_SERVICE_ACCOUNT" in guide


def test_the_guide_upgrades_an_existing_deployment_to_the_sweep():
    """Final review I5: a deployment from before files in answers has no ASKS_SWEEP_SERVICE_ACCOUNT and no job: the
    guide gives both commands, with placeholders and no real values."""
    guide = (ROOT / "deploy" / "hosted" / "README.md").read_text()
    section = guide[guide.index("## Upgrading an existing deployment"):]
    section = section[:section.index("\n## ", 10)]
    assert "gcloud run services update qtracker --region=$REGION --update-env-vars ASKS_SWEEP_SERVICE_ACCOUNT=$SA" in section
    assert "gcloud scheduler jobs create http askhooman-sweep" in section
    assert "--oidc-service-account-email=$SA" in section and "--oidc-token-audience=https://<PUBLIC_HOST>" in section
    assert "ASKS_SWEEP_SERVICE_ACCOUNT is not set" in section  # the warning the bridge logs at start
    # M4: a done claim carries expires_at, and Firestore's TTL policy deletes it (first deploy and upgrade)
    assert "--collection-group=fileclaims --enable-ttl" in section
    assert re.search(r"for c in [a-z ]*\bfileclaims\b[a-z ]*; do", guide)
    assert "<PROJECT>" in section and "<REGION>" in section and "<PUBLIC_HOST>" in section
    assert not re.search(r"[\w.-]+@(?!\$PROJECT)[\w-]+\.iam\.gserviceaccount\.com", section)  # no real account
