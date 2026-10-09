"""
Unit tests of the taskbar alert hooks in module/webui/instance_watchdog.py
(fork): which alert call happens on which watchdog transition.

Same recipe as test_instance_watchdog_gate.py: stub `process_manager` and
`updater` modules, a stub `module.webui.fork_taskbar_alert` whose
`taskbar_alert` records every call, a fake clock on the watchdog's `time`
name, patched `idle_seconds` / `emulator_running`. The working directory is
moved to a temporary directory BEFORE the watchdog is imported, and after
module.logger AND module.webui (deploy.logger chdir's to the repo root too).

Run from the repository root, no pytest needed:
    ./toolkit/python.exe -m tests.fork.test_instance_watchdog_alert
"""
import importlib
import os
import sys
import tempfile
import traceback
import types

import module.logger  # noqa: F401  (chdir to repo root happens here)
import module.webui  # noqa: F401  (deploy.logger chdir's too)

SCRATCH = tempfile.mkdtemp(prefix="alas_fork_watchdog_alert_test_")
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


class StubAlert:
    def __init__(self):
        self.calls = []
        self.restored = []

    def startup(self):
        self.calls.append(("startup",))

    def tick(self):
        self.calls.append(("tick",))

    def crashed(self, name, repeat=False):
        self.calls.append(("crashed", name, repeat))

    def gave_up(self, name):
        self.calls.append(("gave_up", name))

    def recovered(self, name):
        self.calls.append(("recovered", name))

    def names(self):
        return list(self.restored)

    def clear_all(self):
        self.calls.append(("clear_all",))

    def count(self, kind, name=None):
        return sum(1 for c in self.calls if c[0] == kind and (name is None or c[1] == name))


pm_mod = types.ModuleType("module.webui.process_manager")
pm_mod.ProcessManager = FakePM
sys.modules["module.webui.process_manager"] = pm_mod

upd_mod = types.ModuleType("module.webui.updater")
upd_mod.updater = types.SimpleNamespace(state="checking", event=None)
sys.modules["module.webui.updater"] = upd_mod

alert_mod = types.ModuleType("module.webui.fork_taskbar_alert")
alert_mod.taskbar_alert = StubAlert()
sys.modules["module.webui.fork_taskbar_alert"] = alert_mod

import module.webui.instance_watchdog as wd  # noqa: E402


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, text):
        self.lines.append(("info", text))

    def warning(self, text):
        self.lines.append(("warning", text))

    def error(self, text):
        self.lines.append(("error", text))


CRASH = "some scheduler log line\n"
MANUAL = "[x] exited. Reason: Manual stop\n"
FINISH = "[x] exited. Reason: Finish\n"


class Harness:
    def __init__(self, name, sentinel=CRASH):
        self.clock = FakeTime()
        self.log = FakeLogger()
        wd.time = self.clock
        wd.logger = self.log
        wd.idle_seconds = lambda: 600
        wd.emulator_running = lambda n: True
        wd.taskbar_alert = StubAlert()
        self.alert = wd.taskbar_alert
        FakePM._processes.clear()
        FakePM._process_locks.clear()
        self.pm = FakePM(name, sentinel)
        self.watchdog = wd.InstanceWatchdog()

    def tick(self, advance=0):
        self.clock.now += advance
        self.watchdog.check()

    def die(self):
        """The running child exits without a clean-exit sentinel."""
        self.pm.alive = False

    def crash_restart_cycle(self):
        """crash seen -> grace -> watchdog restart -> seen alive -> dies again."""
        self.tick(wd.GRACE_SECONDS + 1)       # restart
        assert self.pm.alive
        self.tick(60)                         # alive branch: recovered
        self.die()


def test_startup_once_before_anything_else():
    h = Harness("x")
    h.tick()
    h.tick(60)
    assert h.alert.calls[0] == ("startup",)
    assert h.alert.count("startup") == 1
    assert h.alert.count("tick") == 2


def test_startup_runs_even_while_updater_is_busy():
    h = Harness("x")
    upd_mod.updater.state = "reload"
    try:
        h.tick()
    finally:
        upd_mod.updater.state = "checking"
    assert h.alert.calls == [("startup",)]      # busy: no tick, no instance check


def test_crash_alerts_then_restart_recovers_next_tick():
    h = Harness("x")
    h.tick()
    assert h.alert.count("crashed", "x") == 1
    assert h.alert.calls[-2] == ("crashed", "x", False)
    h.tick(60)                                  # still down, inside grace
    assert h.alert.count("crashed", "x") == 1   # reported once per sighting
    h.tick(wd.GRACE_SECONDS)                    # restart
    assert h.pm.started == 1
    assert h.alert.count("recovered", "x") == 0 # not in the restart tick
    h.tick(60)
    assert h.alert.count("recovered", "x") == 1


def test_death_after_own_restart_is_repeat():
    h = Harness("x")
    h.tick()
    h.crash_restart_cycle()
    h.tick(60)
    assert h.alert.calls[-2] == ("crashed", "x", True)


def test_gave_up_alerted_once():
    h = Harness("x")
    h.tick()
    for _ in range(wd.RESTART_LIMIT):
        h.crash_restart_cycle()
        h.tick(60)                              # crash seen (repeat)
    assert h.pm.started == wd.RESTART_LIMIT
    assert h.alert.count("gave_up") == 0
    h.tick(wd.GRACE_SECONDS + 1)                # budget spent
    assert h.alert.count("gave_up", "x") == 1
    h.tick(60)
    h.tick(60)
    assert h.alert.count("gave_up", "x") == 1
    assert h.alert.count("crashed", "x") == 1 + wd.RESTART_LIMIT


def test_external_start_recovers_only_when_alive():
    h = Harness("x")
    h.tick()
    h.pm._process = FakeProcess()               # user pressed Start, it died at once
    h.pm.alive = False
    h.tick(60)
    assert h.alert.count("recovered") == 0
    h.pm._process = FakeProcess()               # Start again, this time it runs
    h.pm.alive = True
    h.tick(60)
    assert h.alert.count("recovered", "x") == 1


def test_clean_stops_recover():
    for sentinel in (MANUAL, FINISH):
        h = Harness("x", sentinel)
        h.tick()
        assert h.alert.count("recovered", "x") == 1
        assert h.alert.count("crashed") == 0


def test_restored_alert_cleared_when_instance_runs():
    h = Harness("x")
    h.alert.restored = ["carried"]
    h.tick()
    assert h.alert.count("recovered", "carried") == 0     # no such process yet
    FakePM("carried").alive = True
    h.tick(60)
    assert h.alert.count("recovered", "carried") == 1


def test_broken_alert_module_falls_back_to_noop():
    # Must stay last: it reloads the watchdog module twice.
    saved = sys.modules["module.webui.fork_taskbar_alert"]
    sys.modules["module.webui.fork_taskbar_alert"] = None     # import -> ImportError
    try:
        importlib.reload(wd)
        assert type(wd.taskbar_alert).__name__ == "_NoAlert"
        assert wd.taskbar_alert.names() == []
        h = Harness("x")
        h.alert = wd.taskbar_alert = type(wd.taskbar_alert)()
        h.tick()
        h.tick(wd.GRACE_SECONDS + 1)
        assert h.pm.started == 1                          # watchdog still works
    finally:
        sys.modules["module.webui.fork_taskbar_alert"] = saved
        importlib.reload(wd)


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
