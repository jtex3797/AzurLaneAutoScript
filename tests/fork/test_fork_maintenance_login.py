"""
Unit tests of module/fork_maintenance_login.py (fork).

Run from the repository root, no pytest needed:
    ./toolkit/python.exe -m tests.fork.test_fork_maintenance_login

Importing module.* makes module.logger chdir into the repository root, so the
working directory is moved to a fresh temporary directory right after the
imports and every path below is absolute (CLAUDE.md hard rule 4). No network:
the server status API call (_load_server) is replaced by scripted answers.
"""
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

import module.base.timer as timer_module
import module.fork_maintenance_login as fml
from module.fork_maintenance_login import (
    JST,
    PROBE_INTERVAL,
    PROBE_LEAD,
    PROBE_STALE,
    WAIT_STEP,
    WINDOW_API_INTERVAL,
    ForkServerChecker,
    in_window,
    probe_due,
    read_notice_end,
)

SCRATCH = tempfile.mkdtemp(prefix="alas_fork_probe_test_")
os.chdir(SCRATCH)
STATE = os.path.join(SCRATCH, "fork_maintenance.json")


def jst(*args):
    return datetime(*args, tzinfo=JST)


END = jst(2026, 11, 5, 20, 0)


def write_state(end):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"notice_id": 770, "status": "in_progress", "end": end.isoformat() if end else None}, f)


class Clock:
    """Fake clock shared by the checker, Timer and time.sleep."""

    def __init__(self, start):
        self.now = start

    def epoch(self):
        return self.now.timestamp()

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeChecker(ForkServerChecker):
    """
    answers: list of API answers, True = available. The last one repeats.
    """

    def __init__(self, answers, probe, clock):
        self.answers = list(answers)
        self.api_calls = 0
        self.api_times = []
        self.clock = clock
        super().__init__("jp-15", probe)

    def _load_server(self):
        self.api_calls += 1
        self.api_times.append(self.clock.now)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        self._state.append(answer)

    def _now(self):
        return self.clock.now


class Probe:
    def __init__(self, results, clock=None, cost=60):
        self.results = list(results)
        self.calls = []
        self.clock = clock
        self.cost = cost

    def __call__(self):
        if self.clock is not None:
            self.calls.append(self.clock.now)
            self.clock.sleep(self.cost)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def run_wait(checker, clock, limit_hours=20):
    """
    Run wait_until_available() on the fake clock; fail instead of looping forever.
    """
    deadline = clock.now + timedelta(hours=limit_hours)

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.now > deadline:
            raise TimeoutError("wait_until_available did not return")

    old_sleep, old_time, old_file = fml.time.sleep, timer_module.time, fml.STATE_FILE
    fml.time.sleep = sleep
    timer_module.time = clock.epoch
    fml.STATE_FILE = STATE
    try:
        checker.wait_until_available()
    finally:
        fml.time.sleep, timer_module.time, fml.STATE_FILE = old_sleep, old_time, old_file


def make(answers, results, start, end=END, cost=60):
    write_state(end)
    clock = Clock(start)
    old_time = timer_module.time
    timer_module.time = clock.epoch
    try:
        probe = Probe(results, clock, cost)
        checker = FakeChecker(answers, probe, clock)
    finally:
        timer_module.time = old_time
    return checker, probe, clock


# ---------- pure helpers ----------

def test_read_notice_end():
    write_state(END)
    assert read_notice_end(STATE) == END
    write_state(None)
    assert read_notice_end(STATE) is None
    with open(STATE, "w", encoding="utf-8") as f:
        f.write('{"end": "2026-11-05T2')
    assert read_notice_end(STATE) is None
    assert read_notice_end(os.path.join(SCRATCH, "missing.json")) is None
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"end": "2026-11-05T20:00:00"}, f)
    assert read_notice_end(STATE) is None, "naive time is rejected"


def test_in_window():
    assert not in_window(END, None)
    assert not in_window(END - PROBE_LEAD - timedelta(seconds=1), END)
    assert in_window(END - PROBE_LEAD, END)
    assert in_window(END + timedelta(hours=3), END)
    assert in_window(END + PROBE_STALE, END)
    assert not in_window(END + PROBE_STALE + timedelta(seconds=1), END)


def test_probe_due():
    now = END - timedelta(minutes=20)
    assert probe_due(now, END, None)
    assert not probe_due(now, END, now - timedelta(minutes=9))
    assert probe_due(now, END, now - PROBE_INTERVAL)
    assert not probe_due(now, None, None)
    # Extension: the window moves with the end
    later = END + timedelta(hours=2)
    assert not probe_due(now, later, None)
    assert probe_due(later - PROBE_LEAD, later, None)


# ---------- wait_until_available ----------

def test_api_open_no_probe():
    checker, probe, clock = make([False, False, True], [False], jst(2026, 11, 5, 15, 0))
    run_wait(checker, clock)
    assert probe.calls == []
    assert checker.is_recovered()


def test_no_probe_outside_window():
    # API opens at about 15:30, long before the window (19:30)
    checker, probe, clock = make([False] * 5 + [True], [True], jst(2026, 11, 5, 15, 0))
    run_wait(checker, clock)
    assert probe.calls == []


def test_probe_every_10_min_then_open():
    checker, probe, clock = make([False], [False, False, True], jst(2026, 11, 5, 19, 0))
    run_wait(checker, clock)
    assert len(probe.calls) == 3
    assert probe.calls[0] >= END - PROBE_LEAD
    assert probe.calls[0] < END - PROBE_LEAD + timedelta(seconds=WAIT_STEP + 1)
    for a, b in zip(probe.calls, probe.calls[1:]):
        assert b - a >= PROBE_INTERVAL
        assert b - a < PROBE_INTERVAL + timedelta(seconds=WAIT_STEP + 1)
    assert checker.is_available()
    assert checker.is_recovered()
    assert not checker.is_recovered(), "recovered is reported once"


def test_late_open_keeps_trying():
    # Still closed long after the announced end
    checker, probe, clock = make([False], [False] * 20 + [True], jst(2026, 11, 5, 19, 25))
    run_wait(checker, clock)
    assert len(probe.calls) == 21
    assert probe.calls[-1] > END + timedelta(hours=2)


def test_stale_notice_no_probe():
    checker, probe, clock = make([False] * 5 + [True], [True], jst(2026, 11, 6, 9, 0))
    run_wait(checker, clock)
    assert probe.calls == []


def test_end_none_no_probe():
    checker, probe, clock = make([False] * 5 + [True], [True], jst(2026, 11, 5, 19, 45), end=None)
    run_wait(checker, clock)
    assert probe.calls == []


def test_probe_exception_keeps_waiting():
    checker, probe, clock = make([False], [RuntimeError("boom"), True], jst(2026, 11, 5, 19, 35))
    run_wait(checker, clock)
    assert len(probe.calls) == 2
    assert checker.is_recovered()


def test_api_interval_capped_in_window():
    checker, probe, clock = make([False] * 40 + [True], [False], jst(2026, 11, 5, 18, 0), cost=0)
    run_wait(checker, clock)
    times = checker.api_times
    gaps = [(b, (b - a).total_seconds()) for a, b in zip(times, times[1:])]
    before = [g for t, g in gaps if t < END - PROBE_LEAD]
    inside = [g for t, g in gaps if t > END - PROBE_LEAD + timedelta(minutes=11)]
    assert max(before) >= 600
    assert inside and max(inside) <= WINDOW_API_INTERVAL + WAIT_STEP


def test_notice_moved_while_waiting():
    # The notice moves the end from 20:00 to 22:00 before the window opens
    checker, probe, clock = make([False], [True], jst(2026, 11, 5, 19, 0))
    write_state(jst(2026, 11, 5, 22, 0))
    run_wait(checker, clock)
    assert probe.calls[0] >= jst(2026, 11, 5, 21, 30)


def test_trust_probe_once():
    checker, probe, clock = make([False], [True], jst(2026, 11, 5, 19, 45))
    run_wait(checker, clock)
    assert checker.is_available()
    # loop() reads the recovery right after the wait and queues Restart
    assert checker.is_recovered()
    old_time = timer_module.time
    timer_module.time = clock.epoch
    try:
        # GamePageUnknownError: run() asks the API, then loop() again 10 s later,
        # both still "maintenance" from a lagging API, both trusted
        checker.check_now()
        assert checker.is_available()
        clock.sleep(10)
        checker.check_now()
        assert checker.is_available()
        assert not checker.is_recovered(), "trust does not queue another Restart"
        # Past the grace the API is followed again
        clock.sleep(60)
        checker.check_now()
        assert not checker._state[-1]
        # and the trust is used up
        clock.sleep(1)
        checker.check_now()
        assert not checker._state[-1]
    finally:
        timer_module.time = old_time


def test_trust_expires():
    checker, probe, clock = make([False], [True], jst(2026, 11, 5, 19, 45))
    run_wait(checker, clock)
    clock.sleep(31 * 60)
    old_time = timer_module.time
    timer_module.time = clock.epoch
    try:
        checker.check_now()
        assert not checker._state[-1]
    finally:
        timer_module.time = old_time


def test_maintenance_probe():
    """ForkAzurLaneAutoScript.maintenance_probe() with run_probe and the emulator check stubbed."""
    import module.fork_recovery as fork_recovery
    import module.webui.emulator_probe as emulator_probe
    from module.fork_recovery import ForkAzurLaneAutoScript

    class Config:
        Emulator_PackageName = "com.YoStarJP.AzurLane"
        Emulator_ServerName = "jp-15"

    class FakeDatetime:
        now_value = datetime(2026, 11, 5, 19, 30)

        @classmethod
        def now(cls):
            return cls.now_value

    calls = []

    def fake_run_probe(config, device, keep_running=False):
        calls.append(keep_running)
        return results.pop(0)

    def advance(minutes):
        FakeDatetime.now_value += timedelta(minutes=minutes)

    old = fml.run_probe, emulator_probe.emulator_running, fork_recovery.datetime
    fml.run_probe = fake_run_probe
    fork_recovery.datetime = FakeDatetime
    try:
        # A real instance, so a missing attribute in __init__ shows up here
        bot = ForkAzurLaneAutoScript(config_name="fork_probe_test")
        bot.__dict__["config"] = Config()
        bot.__dict__["device"] = object()
        probe = bot.maintenance_probe

        emulator_probe.emulator_running = lambda name: False
        results = [(True, False)]
        assert probe() is False and calls == [], "emulator off"

        emulator_probe.emulator_running = lambda name: True
        # Download clicked, still closed; the next probes within 30 min keep the game running
        results = [(False, True), (False, False), (False, False), (False, False)]
        assert probe() is False
        advance(10)
        assert probe() is False
        advance(10)
        assert probe() is False
        advance(11)
        assert probe() is False
        assert calls == [False, True, True, False], calls

        # Clicked again, then open: the flag is cleared
        results = [(False, True), (True, False), (False, False)]
        assert probe() is False
        advance(10)
        assert probe() is True
        advance(10)
        assert probe() is False
        assert calls[-3:] == [False, True, False], calls

        # Unknown emulator state and no device yet: don't create one
        emulator_probe.emulator_running = lambda name: None
        bot2 = ForkAzurLaneAutoScript(config_name="fork_probe_test")
        bot2.__dict__["config"] = Config()
        count = len(calls)
        assert bot2.maintenance_probe() is False and len(calls) == count
        # Unknown but a device exists: try
        results = [(False, False)]
        assert probe() is False and len(calls) == count + 1

        # Non-JP instance
        bot.__dict__["config"] = type("C", (), {
            "Emulator_PackageName": "com.bilibili.azurlane", "Emulator_ServerName": "cn-1"})()
        emulator_probe.emulator_running = lambda name: True
        assert probe() is False and len(calls) == count + 1
        bot.__dict__["config"] = Config()

        # run_probe raising never escapes
        def boom(*a, **k):
            raise RuntimeError("adb gone")
        fml.run_probe = boom
        assert probe() is False
    finally:
        fml.run_probe, emulator_probe.emulator_running, fork_recovery.datetime = old


class FakeDevice:
    def __init__(self):
        self.log = []
        self.shots = 0

    def app_stop(self):
        self.log.append("stop")

    def app_start(self):
        self.log.append("start")

    def screenshot(self):
        self.shots += 1

    def sleep(self, seconds):
        pass

    def stuck_record_clear(self):
        self.log.append("stuck_clear")

    def click_record_clear(self):
        self.log.append("click_clear")


class ProbeConfig:
    is_actual_task = False


class NamedButton:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


def test_run_probe():
    """run_probe() with the upstream login replaced, no device, no game."""
    import module.base.base as base
    import module.handler.login as login
    from module.exception import GameTooManyClickError

    old = (login.LoginHandler.handle_app_login, login.LoginHandler.is_in_main,
           base.ModuleBase.appear_then_click, fml.PROBE_TIMEOUT)
    state = {"main": True}

    def no_main_attr(device):
        return "screenshot" not in device.__dict__

    try:
        login.LoginHandler.is_in_main = lambda self, *a, **k: state["main"]
        base.ModuleBase.appear_then_click = lambda self, button, *a, **k: True

        # Login reaches page_main: open, game restarted first
        def reach_main(self):
            self.device.screenshot()
        login.LoginHandler.handle_app_login = reach_main
        device = FakeDevice()
        assert fml.run_probe(ProbeConfig(), device) == (True, False)
        assert device.log[:2] == ["stop", "start"] and no_main_attr(device)
        assert device.log[-2:] == ["stuck_clear", "click_clear"]

        # Returned after a popup but not on page_main: closed
        state["main"] = False
        device = FakeDevice()
        assert fml.run_probe(ProbeConfig(), device) == (False, False)
        state["main"] = True

        # Maintenance: the upstream login ends in a click error; keep_running skips the stop
        def too_many(self):
            self.device.screenshot()
            raise GameTooManyClickError("LOGIN_CHECK")
        login.LoginHandler.handle_app_login = too_many
        device = FakeDevice()
        assert fml.run_probe(ProbeConfig(), device, keep_running=True) == (False, False)
        assert "stop" not in device.log and no_main_attr(device)

        # Patch download offered on the way
        def download(self):
            self.appear_then_click(NamedButton("LOGIN_GAME_UPDATE"), interval=5)
            raise GameTooManyClickError("LOGIN_CHECK")
        login.LoginHandler.handle_app_login = download
        assert fml.run_probe(ProbeConfig(), FakeDevice()) == (False, True)

        # Over the time cap
        fml.PROBE_TIMEOUT = -1
        login.LoginHandler.handle_app_login = reach_main
        device = FakeDevice()
        assert fml.run_probe(ProbeConfig(), device) == (False, False)
        assert device.shots == 0 and no_main_attr(device)
        fml.PROBE_TIMEOUT = old[3]

        # Anything else propagates to maintenance_probe(), still restoring the device
        def broken(self):
            raise ValueError("adb")
        login.LoginHandler.handle_app_login = broken
        device = FakeDevice()
        try:
            fml.run_probe(ProbeConfig(), device)
            raise AssertionError("ValueError swallowed")
        except ValueError:
            pass
        assert no_main_attr(device)
    finally:
        (login.LoginHandler.handle_app_login, login.LoginHandler.is_in_main,
         base.ModuleBase.appear_then_click, fml.PROBE_TIMEOUT) = old


def test_constructor_check_now():
    # check_now() runs inside ServerChecker.__init__, before the fork fields would exist
    clock = Clock(jst(2026, 11, 5, 19, 45))
    FakeChecker([False], Probe([True], clock), clock)
    FakeChecker([True], Probe([True], clock), clock)


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
