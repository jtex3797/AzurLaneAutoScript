import re
from datetime import datetime, timezone

import requests

from module.webui.setting import State


class WorkflowChecker:
    """
    Checks the latest run of the fork's upstream-sync workflow on GitHub
    and exposes its state for the GUI to display.

    state:
        "ok"       latest run succeeded recently
        "failure"  latest run failed
        "stale"    no run within STALE_SECONDS (cron disabled or stuck)
        "unknown"  not checked yet / network error / non-github repository
    """

    WORKFLOW_FILE = "sync-upstream.yml"
    STALE_SECONDS = 6 * 3600

    def __init__(self):
        self.state = "unknown"

    def check(self):
        # Must never raise: TaskHandler.loop() permanently removes a task
        # that raises, which would silently kill this checker until restart.
        try:
            repo = str(State.deploy_config.Repository).strip().rstrip("/")
            # config_redirect() may rewrite Repository to a non-github URL
            # ('git://git.lyoko.io/...') or shorthands ('cn', 'global')
            m = re.match(r"^https://github\.com/([^/]+)/([^/]+)$", repo)
            if not m:
                self.state = "unknown"
                return
            resp = requests.get(
                f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}"
                f"/actions/workflows/{self.WORKFLOW_FILE}/runs?per_page=1",
                headers={"User-Agent": "ALAS-fork-sync-checker"},
                timeout=(5, 10),
            )
            if resp.status_code != 200:
                self.state = "unknown"
                return
            runs = resp.json().get("workflow_runs", [])
            if not runs:
                self.state = "stale"
                return
            run = runs[0]
            created = datetime.strptime(
                run["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if run.get("conclusion") == "failure":
                self.state = "failure"
            elif age > self.STALE_SECONDS:
                self.state = "stale"
            else:
                self.state = "ok"
        except Exception:
            self.state = "unknown"


workflow_checker = WorkflowChecker()
