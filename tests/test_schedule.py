from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "market-scan.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_schedule_is_six_am_weekdays_in_california() -> None:
    text = _workflow_text()
    assert re.search(r"cron:\s*[\"']0 6 \* \* 1-5[\"']", text)
    assert re.search(r"timezone:\s*[\"']America/Los_Angeles[\"']", text)


def test_workflow_is_manually_dispatchable_and_least_privilege() -> None:
    text = _workflow_text()
    assert re.search(r"^\s{2}workflow_dispatch:\s*$", text, re.MULTILINE)
    assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE)
    assert re.search(r"^  cancel-in-progress: false$", text, re.MULTILINE)
    assert re.search(r"options:\n\s+- alpaca\n\s+- yahoo\n\s+- demo", text)


def test_scheduled_provider_is_configurable_without_editing_the_workflow() -> None:
    text = _workflow_text()
    assert "SCAN_PROVIDER: ${{ inputs.provider || vars.SCAN_PROVIDER || 'alpaca' }}" in text
    assert 'market-scanner scan --provider "$SCAN_PROVIDER"' in text
    # Alpaca credentials must still be enforced when Alpaca is the provider.
    assert "if: env.SCAN_PROVIDER == 'alpaca'" in text


def test_watchlist_is_published_to_the_run_summary() -> None:
    text = _workflow_text()
    assert 'cat "$watchlist" >> "$GITHUB_STEP_SUMMARY"' in text
    # The summary must still say something when no report was written.
    assert "No watchlist produced" in text


def test_workflow_tests_scans_and_uploads_artifacts() -> None:
    text = _workflow_text()
    assert "timeout-minutes: 30" in text
    assert "python -m pytest" in text
    assert "market-scanner scan" in text
    assert "uses: actions/upload-artifact@v7.0.1" in text


def test_credentials_are_scoped_to_steps_not_the_entire_job() -> None:
    text = _workflow_text()
    job_env = text.split("    env:\n", maxsplit=1)[1].split("    steps:\n", maxsplit=1)[0]
    assert "APCA_API_KEY_ID" not in job_env
    assert "APCA_API_SECRET_KEY" not in job_env
    assert "secrets.APCA_API_KEY_ID" in text
    assert "secrets.APCA_API_SECRET_KEY" in text


def test_launchd_installer_generates_weekdays_at_six() -> None:
    script = (ROOT / "scripts" / "install_launchd.sh").read_text(encoding="utf-8")
    assert '"Hour": 6' in script
    assert '"Minute": 0' in script
    assert "range(1, 6)" in script
    assert "StartCalendarInterval" in script
