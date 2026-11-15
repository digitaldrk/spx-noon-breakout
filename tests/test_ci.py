"""The GitHub Actions workflow: two checks, no secrets, and no second deploy path.

`.github/workflows/ci.yml` is what gates the Render deploy, because
`render.yaml` carries `autoDeployTrigger: checksPass`. So the shape of that file
is a contract, not a preference: the check names are what branch protection and
Render look for, the Python version is the image's, and the workflow has to stay
free of credentials so that what runs in Actions is the same isolated suite that
runs on a laptop (`conftest.py`).

Nothing here talks to GitHub or Render. It reads the file, the same way
`test_render.py` reads `render.yaml`, and reuses that module's small YAML reader.
"""
from pathlib import Path

from test_render import PLAIN_NAMES, SECRET_NAMES, _yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"

#: The reported check names, which are also the job ids. Branch protection on
#: `main` requires these two spellings exactly.
CHECKS = ("test", "docker")

#: The image's interpreter. The Dockerfile's `FROM python:3.11-slim`, pinned by
#: ``test_the_image_has_no_port_no_health_check_and_copies_no_state``.
PYTHON = "3.11"

#: What the Dockerfile installs, plus pytest.
EXTRA = "'.[tasty,test]'"

#: Names no Actions runner may be handed: the launchers' and the worker's own
#: variables, the five dashboard secrets, and the sheets credential blob.
FORBIDDEN_NAMES = (*SECRET_NAMES, *PLAIN_NAMES, "TASTYTRADE_", "SPX_NOON_", "TASTY_",
                   "SPX_SHEETS_CREDENTIALS_JSON")


def _workflow():
    parsed = _yaml(CI.read_text())
    assert list(parsed) == ["on", "permissions", "jobs"], list(parsed)
    return parsed


def _job(name):
    job = _workflow()["jobs"][name]
    assert job["runs-on"] == "ubuntu-latest"
    return job


def _branches(raw):
    """`[main]` as a list. The reader hands a flow sequence back as a string."""
    assert raw.startswith("[") and raw.endswith("]"), raw
    return [item.strip().strip("\"'") for item in raw[1:-1].split(",")]


def _runs(job):
    """Every shell command in a job, in order."""
    return [step["run"] for step in job["steps"] if "run" in step]


def _uses(job):
    return [step["uses"] for step in job["steps"] if "uses" in step]


# --------------------------------------------------------------------------
# When it runs, and what the checks are called
# --------------------------------------------------------------------------

def test_one_workflow_runs_on_pull_requests_and_on_main_and_on_nothing_else():
    assert sorted(path.name for path in WORKFLOWS.iterdir()) == ["ci.yml"]
    triggers = _workflow()["on"]
    assert list(triggers) == ["pull_request", "push"]
    assert triggers["pull_request"] is None            # every pull request, no filter
    assert list(triggers["push"]) == ["branches"]
    assert _branches(triggers["push"]["branches"]) == ["main"]
    # no schedule, no manual run, no tag or release trigger
    text = CI.read_text()
    for absent in ("workflow_dispatch", "schedule:", "cron", "release:", "tags:",
                   "workflow_run", "repository_dispatch"):
        assert absent not in text, absent


def test_the_checks_github_reports_are_exactly_test_and_docker():
    """The job `name` is the check name only while the workflow itself has none.

    A top-level `name: CI` would make GitHub report `CI / test`, which branch
    protection cannot require as `test`.
    """
    parsed = _workflow()
    assert "name" not in parsed
    assert "name:" not in CI.read_text().splitlines()[0]
    jobs = parsed["jobs"]
    assert list(jobs) == list(CHECKS)
    for check in CHECKS:
        assert jobs[check]["name"] == check
        assert "needs" not in jobs[check]              # neither waits on the other


def test_the_workflow_can_only_read_the_repository():
    parsed = _workflow()
    assert parsed["permissions"] == {"contents": "read"}
    # a job-level block would widen the token past the top-level one
    for check in CHECKS:
        assert "permissions" not in parsed["jobs"][check]
    assert "contents: write" not in CI.read_text()


def test_neither_check_can_be_skipped_or_turned_into_advice():
    """A green check has to mean the step ran and passed.

    `continue-on-error` reports success on a failed step, and an `if:` can skip
    a job outright. Either one would let Render deploy an image that does not
    build, or a commit whose tests fail, with both checks showing green.
    """
    jobs = _workflow()["jobs"]
    for check in CHECKS:
        job = jobs[check]
        for escape in ("if", "continue-on-error"):
            assert escape not in job, (check, escape)
            for step in job["steps"]:
                assert escape not in step, (check, escape)
    text = CI.read_text()
    for absent in ("continue-on-error", "if:", "|| true", "|| exit 0"):
        assert absent not in text, absent


# --------------------------------------------------------------------------
# The two jobs
# --------------------------------------------------------------------------

def test_the_test_check_runs_the_suite_on_the_images_python():
    job = _job("test")
    setup = next(step for step in job["steps"] if step.get("uses", "").startswith(
        "actions/setup-python"))
    assert setup["with"] == {"python-version": PYTHON}
    # the same interpreter installs the package and runs pytest
    assert _runs(job) == [f"python -m pip install {EXTRA}", "python -m pytest -q"]


def test_the_docker_check_builds_the_image_and_pushes_nothing():
    job = _job("docker")
    assert _runs(job) == ["docker build ."]
    assert _uses(job) == ["actions/checkout@v5"]
    text = CI.read_text()
    for absent in ("docker push", "docker tag", "docker login", "docker/login-action",
                   "docker/build-push-action", "docker/setup-buildx-action",
                   "docker/metadata-action", "ghcr.io", "docker.io", "push: true"):
        assert absent not in text, absent


def test_the_actions_are_pinned_to_a_major_tag_that_exists():
    """`actions/checkout@v5` and `actions/setup-python@v5`, never a floating branch."""
    used = _uses(_job("test")) + _uses(_job("docker"))
    assert used == ["actions/checkout@v5", "actions/setup-python@v5",
                    "actions/checkout@v5"]
    for action in used:
        assert "@main" not in action and "@master" not in action


# --------------------------------------------------------------------------
# No credentials, and no way to deploy from here
# --------------------------------------------------------------------------

def test_the_workflow_is_handed_no_broker_or_google_credentials():
    """CI runs the isolated suite, so there is nothing for it to be handed.

    `conftest.py` strips the broker environment inside every test anyway. This
    keeps the runner itself clean, so a workflow edit cannot quietly put a live
    credential in front of the suite.
    """
    text = CI.read_text()
    assert "${{" not in text                           # no expression context at all
    assert "secrets." not in text
    for name in FORBIDDEN_NAMES:
        assert name not in text, name
    parsed = _workflow()
    assert "env" not in parsed
    for check in CHECKS:
        job = parsed["jobs"][check]
        assert "env" not in job
        for step in job["steps"]:
            assert "env" not in step


def test_actions_is_not_a_second_deploy_path():
    """Render deploys main itself once the checks pass. Nothing here deploys."""
    text = CI.read_text()
    for absent in ("api.render.com", "RENDER_API_KEY", "RENDER_SERVICE_ID", "deploy-hook",
                   "deploy_hook", "curl", "srv-"):
        assert absent not in text, absent


# --------------------------------------------------------------------------
# The other half of the gate
# --------------------------------------------------------------------------

def test_the_blueprint_waits_for_these_two_checks():
    """The blueprint's trigger is only meaningful next to this workflow.

    The worker's keys are pinned in `test_render.py`; this is the join between
    the two files.
    """
    blueprint = (ROOT / "render.yaml").read_text()
    assert "autoDeployTrigger: checksPass" in blueprint
    assert "autoDeploy" not in blueprint.replace("autoDeployTrigger", "")
    assert "branch:" not in blueprint                  # Render follows the default branch
    doc = (ROOT / "RENDER.md").read_text()
    for needle in ("checksPass", "`test`", "`docker`", ".github/workflows/ci.yml",
                   "11:45 and 15:10"):
        assert needle in doc, needle
    assert "`autoDeploy` is off" not in doc
