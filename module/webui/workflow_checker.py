import re
import threading
from datetime import datetime, timezone

import requests

from module.webui.setting import State


class WorkflowChecker:
    """
    Tells the GUI whether the fork is still receiving upstream commits.

    Judged on what actually matters - are upstream commits missing from this
    fork - rather than on how long ago sync-upstream.yml last ran. GitHub
    drops scheduled runs under load, so the observed interval of an hourly
    cron swings between 2 and 6.3 hours, and a run-recency rule raises false
    alarms whenever upstream happens to be quiet.

    state:
        "ok"       nothing missing, or missing for less than STALE_SECONDS
        "failure"  the latest sync run failed (merge conflict, rejected push)
        "stale"    upstream commits unmerged for longer than STALE_SECONDS
        "unknown"  not checked yet / network error / non-github repository
    """

    WORKFLOW_FILE = "sync-upstream.yml"
    # The repository sync-upstream.yml merges from.
    UPSTREAM = "LmeSzinc/AzurLaneAutoScript"
    UPSTREAM_BRANCH = "master"
    STALE_SECONDS = 24 * 3600
    # The compare API returns at most 250 commits; past that, commits[0] is
    # no longer the oldest unmerged one.
    COMPARE_LIMIT = 250
    # Lower bound between two manual re-checks from the status popup, so
    # repeated clicks cannot burn the unauthenticated quota (60 requests/h).
    RECHECK_SECONDS = 60

    def __init__(self):
        self.state = "unknown"
        # Filled in by check(); None means "not known right now", which the
        # popup renders as '-'.
        self.behind = None
        self.lag_seconds = None
        self.run_at = None
        self.run_conclusion = None
        self.run_url = None
        self.workflow_url = None
        self.checked_at = None
        self._lock = threading.Lock()

    def check(self):
        # Must never raise: TaskHandler.loop() permanently removes a task
        # that raises, which would silently kill this checker until restart.
        if not self._lock.acquire(False):
            # The hourly check and a manual re-check from the popup would
            # otherwise interleave and leave the fields below inconsistent.
            return
        try:
            self._check()
        except Exception:
            self.state = "unknown"
        finally:
            self._lock.release()

    def recheck(self):
        """
        Manual re-check from the status popup.

        Returns:
            bool: False if throttled, nothing was checked.
        """
        if self.checked_at is not None:
            age = (datetime.now(timezone.utc) - self.checked_at).total_seconds()
            if age < self.RECHECK_SECONDS:
                return False
        self.check()
        return True

    def _check(self):
        # Reset first: every early return below would otherwise leave the
        # previous call's numbers on display.
        self.behind = None
        self.lag_seconds = None
        self.run_at = None
        self.run_conclusion = None
        self.run_url = None
        self.workflow_url = None
        self.checked_at = datetime.now(timezone.utc)

        repo = str(State.deploy_config.Repository).strip().rstrip("/")
        # config_redirect() may rewrite Repository to a non-github URL
        # ('git://git.lyoko.io/...') or shorthands ('cn', 'global')
        m = re.match(r"^https://github\.com/([^/]+)/([^/]+)$", repo)
        if not m:
            self.state = "unknown"
            return
        owner, name = m.group(1), m.group(2)
        self.workflow_url = f"{repo}/actions/workflows/{self.WORKFLOW_FILE}"

        data = self._get(
            f"https://api.github.com/repos/{owner}/{name}"
            f"/actions/workflows/{self.WORKFLOW_FILE}/runs?per_page=1"
        )
        if data is None:
            self.state = "unknown"
            return
        runs = data.get("workflow_runs", [])
        if runs:
            self.run_at = self._time(runs[0].get("created_at"))
            self.run_conclusion = runs[0].get("conclusion")
            self.run_url = runs[0].get("html_url")
            if self.run_conclusion == "failure":
                # A conflict or a rejected push needs a human whatever the
                # commit comparison says.
                self.state = "failure"
                return

        branch = str(State.deploy_config.Branch).strip() or "master"
        upstream = self.UPSTREAM.replace("/", ":")
        compare = self._get(
            f"https://api.github.com/repos/{owner}/{name}/compare/"
            f"{branch}...{upstream}:{self.UPSTREAM_BRANCH}"
        )
        if compare is None:
            # Network error or rate limit: run recency is the only signal left.
            self.state = self._state_by_run()
            return

        # ahead_by counts commits the head (upstream) has and the base (this
        # fork) does not. behind_by counts this fork's own commits, which are
        # expected and mean nothing here.
        self.behind = compare.get("ahead_by", 0)
        if not self.behind:
            self.state = "ok"
            return
        if self.behind > self.COMPARE_LIMIT:
            # commits[] is truncated, so the lag cannot be measured - but
            # being this far behind is stale by any standard.
            self.state = "stale"
            return

        commits = compare.get("commits", [])
        oldest = None
        if commits:
            oldest = self._time(
                commits[0].get("commit", {}).get("committer", {}).get("date")
            )
        if oldest is None:
            self.state = self._state_by_run()
            return
        self.lag_seconds = (self.checked_at - oldest).total_seconds()
        self.state = "stale" if self.lag_seconds > self.STALE_SECONDS else "ok"

    def _state_by_run(self):
        if self.run_at is None:
            return "stale"
        age = (datetime.now(timezone.utc) - self.run_at).total_seconds()
        return "stale" if age > self.STALE_SECONDS else "ok"

    def _get(self, url):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "ALAS-fork-sync-checker"},
                timeout=(5, 10),
            )
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _time(value):
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            return None


workflow_checker = WorkflowChecker()
