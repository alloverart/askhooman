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
