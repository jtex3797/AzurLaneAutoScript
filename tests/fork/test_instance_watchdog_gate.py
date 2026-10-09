"""
Unit tests of the emulator gate in module/webui/instance_watchdog.py (fork).

The watchdog is loaded with stub `module.webui.process_manager` and
`module.webui.updater` modules (recipe in .claude/rules/recovery.md), a stub
`module.webui.fork_taskbar_alert` that swallows every alert call, a fake
clock on its `time` name and patched `idle_seconds` / `emulator_running`.

Order matters: importing module.logger chdir's into the repository root, so
the working directory is moved to a temporary directory BEFORE the watchdog
is imported (its import writes ./log/fork_manual_stop.json) and AFTER
module.webui, whose deploy.logger import chdir's to the repo root as well.

Run from the repository root, no pytest needed:
    ./toolkit/python.exe -m tests.fork.test_instance_watchdog_gate
"""
import os
import sys
import tempfile
import threading
import traceback
import types

import module.logger  # noqa: F401  (chdir to repo root happens here)
import module.webui  # noqa: F401  (deploy.logger chdir's too, see recovery.md)

SCRATCH = tempfile.mkdtemp(prefix="alas_fork_watchdog_test_")
os.chdir(SCRATCH)
os.makedirs("log", exist_ok=True)
os.makedirs("config", exist_ok=True)


# ---------- stubs, installed before the watchdog import ----------

class FakeProcess:
    pass


class FakePM:
    _processes = {}
    _process_locks = {}

    def __init__(self, name, sentinel=None):
        self.config_name = name
        self._process = FakeProcess()
        self.alive = False
        self.renderables = [sentinel] if sentinel else []
        self.started = 0
        FakePM._processes[name] = self

    def start(self, func, ev):
        self.started += 1
        self._process = FakeProcess()
        self.alive = True

    @classmethod
    def get_manager(cls, name):
        return cls._processes[name]


pm_mod = types.ModuleType("module.webui.process_manager")
pm_mod.ProcessManager = FakePM
sys.modules["module.webui.process_manager"] = pm_mod

upd_mod = types.ModuleType("module.webui.updater")
upd_mod.updater = types.SimpleNamespace(state="checking", event=None)
sys.modules["module.webui.updater"] = upd_mod

alert_mod = types.ModuleType("module.webui.fork_taskbar_alert")
alert_mod.taskbar_alert = types.SimpleNamespace(
    startup=lambda: None, tick=lambda: None, crashed=lambda name, repeat=False: None,
    gave_up=lambda name: None, recovered=lambda name: None, names=lambda: [],
    clear_all=lambda: None,
)
sys.modules["module.webui.fork_taskbar_alert"] = alert_mod

import module.webui.instance_watchdog as wd  # noqa: E402


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _log(self, level, text):
        self.lines.append((level, text))

    def info(self, text):
        self._log("info", text)

    def warning(self, text):
        self._log("warning", text)

    def error(self, text):
        self._log("error", text)


class Harness:
    """
    One watchdog with its own clock, logger, emulator verdict and idle time.
    """

    def __init__(self, name, sentinel=None):
        self.clock = FakeTime()
        self.log = FakeLogger()
        self.verdict = True
        wd.time = self.clock
        wd.logger = self.log
        wd.idle_seconds = lambda: 600
        wd.emulator_running = lambda n: self.verdict() if callable(self.verdict) else self.verdict
        FakePM._processes.clear()
        FakePM._process_locks.clear()
        self.pm = FakePM(name, sentinel)
        self.watchdog = wd.InstanceWatchdog()
        self.watchdog.manual_resume = True

    def tick(self, advance=0):
        self.clock.now += advance
        self.watchdog.check()

    def count(self, level, needle):
        return sum(1 for lv, t in self.log.lines if lv == level and needle in t)


MANUAL = "[x] exited. Reason: Manual stop\n"


def test_manual_resume_waits_for_emulator_then_settles():
    h = Harness("fake_manual", MANUAL)
    h.tick()                       # records the manual stop
    h.verdict = False
    h.tick(wd.MANUAL_RESUME_SECONDS + 1)
    assert h.pm.started == 0
    assert "fake_manual" in h.watchdog._deferred
    assert h.count("warning", "emulator not running") == 1
    h.tick(60)
    assert h.pm.started == 0
    assert h.count("warning", "emulator not running") == 1      # logged once
    h.verdict = True
    h.tick(60)                     # emulator back: settle timer starts
    assert h.pm.started == 0
    h.tick(wd.BOOT_SETTLE_SECONDS - 1)
    assert h.pm.started == 0
    h.tick(2)
    assert h.pm.started == 1
    assert "fake_manual" not in h.watchdog._deferred
    assert h.count("info", "emulator back") == 1


def test_manual_stop_count_is_not_reset_by_deferral():
    h = Harness("fake_count", MANUAL)
    h.tick()
    since = h.watchdog._manual["fake_count"]
    h.verdict = False
    h.tick(wd.MANUAL_RESUME_SECONDS + 1)
    h.tick(300)
    assert h.watchdog._manual["fake_count"] == since


def test_crash_restart_waits_without_spending_budget():
    # Ordinary output without a clean-exit sentinel = crashed (no output at
    # all would mean "never ran" and is ignored by the watchdog)
    h = Harness("fake_crash", "some scheduler log line" + chr(10))
    h.tick()                          # state3_since recorded
    rec = h.watchdog._tracked["fake_crash"]
    assert rec["state3_since"] is not None
    h.verdict = False
    h.tick(wd.GRACE_SECONDS + 1)
    assert h.pm.started == 0
    assert rec["restarts"] == []
    assert rec["state3_since"] is not None
    h.tick(60)
    assert h.pm.started == 0
    h.verdict = True
    h.tick(60)                        # settle starts
    assert h.pm.started == 0
    h.tick(wd.BOOT_SETTLE_SECONDS + 1)
    assert h.pm.started == 1
    assert len(rec["restarts"]) == 1


def test_unknown_verdict_passes_and_clears_deferral():
    h = Harness("fake_unknown", MANUAL)
    h.tick()
    h.verdict = False
    h.tick(wd.MANUAL_RESUME_SECONDS + 1)
    assert h.pm.started == 0
    h.verdict = None
    h.tick(60)
    assert h.pm.started == 1
    assert not h.watchdog._deferred and not h.watchdog._up_since


def test_running_from_the_start_passes_immediately():
    h = Harness("fake_up", MANUAL)
    h.tick()
    h.verdict = True
    h.tick(wd.MANUAL_RESUME_SECONDS + 1)
    assert h.pm.started == 1
    assert h.count("warning", "emulator not running") == 0


def test_external_start_clears_deferral():
    h = Harness("fake_ext", MANUAL)
    h.tick()
    h.verdict = False
    h.tick(wd.MANUAL_RESUME_SECONDS + 1)
    assert "fake_ext" in h.watchdog._deferred
    h.pm._process = FakeProcess()     # user pressed Start
    h.pm.alive = True
    h.tick(60)
    assert "fake_ext" not in h.watchdog._deferred


def test_probe_exception_never_breaks_check():
    h = Harness("fake_boom", MANUAL)
    h.tick()

    def boom():
        raise RuntimeError("probe broke")

    h.verdict = boom
    h.tick(wd.MANUAL_RESUME_SECONDS + 1)   # must not raise
    assert h.pm.started == 1               # unknown -> old behaviour
    assert h.count("warning", "emulator probe failed") == 1


if __name__ == "__main__":
    failures = 0
    names = [n for n in list(globals()) if n.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(names) - failures}/{len(names)} passed, scratch {SCRATCH}")
    sys.exit(1 if failures else 0)
