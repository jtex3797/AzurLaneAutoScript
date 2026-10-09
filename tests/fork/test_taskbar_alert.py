"""
Unit tests of module/webui/fork_taskbar_alert.py (fork). No Win32, COM or
PowerShell is touched: a FakeBackend records the calls and runs inline.

Order matters: importing module.logger chdir's into the repository root and
so does deploy.logger (pulled in by the module.webui package), so both are
imported first and the working directory is moved to a temporary directory
BEFORE the module under test is imported; that import must not create a file.

Run from the repository root, no pytest needed:
    ./toolkit/python.exe -m tests.fork.test_taskbar_alert
"""
import json
import os
import sys
import tempfile
import traceback

import module.logger  # noqa: F401  (chdir to repo root happens here)
import module.webui  # noqa: F401  (deploy.logger chdir's too)

SCRATCH = tempfile.mkdtemp(prefix="alas_fork_alert_test_")
os.chdir(SCRATCH)
os.makedirs("log")
os.makedirs("config")
BEFORE_IMPORT = sorted(os.listdir("log")) + sorted(os.listdir("config"))

import module.webui.fork_taskbar_alert as ta  # noqa: E402

AFTER_IMPORT = sorted(os.listdir("log")) + sorted(os.listdir("config"))


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.hwnd = 1234
        self.visible = True
        self.fail_overlay = False
        self.overlay_attempts = 0

    def find_window(self):
        return self.hwnd

    def is_visible(self, hwnd):
        return self.visible

    def flash(self, hwnd, on):
        self.calls.append(("flash", on))

    def set_overlay(self, hwnd, on):
        self.overlay_attempts += 1
        if self.fail_overlay:
            raise OSError("ITaskbarList3::HrInit failed, hr=0x80004005")
        self.calls.append(("overlay", on))

    def toast(self, title, body, long=False):
        self.calls.append(("toast", long))


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, text):
        self.lines.append(("info", text))

    def warning(self, text):
        self.lines.append(("warning", text))

    def count(self, level, needle):
        return sum(1 for lv, t in self.lines if lv == level and needle in t)


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def make(reloaded=False, settings=None, state=None):
    """Fresh alert with fake backend, logger and clock; files reset."""
    for name in (ta.STATE_FILE, ta.STATE_FILE + ".tmp", ta.SETTINGS_FILE):
        if os.path.exists(name):
            os.remove(name)
    for name in os.listdir("config"):
        os.remove(os.path.join("config", name))
    if settings is not None:
        with open(ta.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write(settings)
    if state is not None:
        with open(ta.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    ta.RELOADED = reloaded
    ta.logger = FakeLogger()
    ta.time = FakeTime()
    backend = FakeBackend()
    return ta.TaskbarAlert(backend=backend), backend, ta.logger, ta.time


def saved():
    with open(ta.STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def test_import_has_no_side_effect():
    assert BEFORE_IMPORT == AFTER_IMPORT == []


def test_dot_pixels_red_disc_with_white_rim():
    size = 16
    px = ta._dot_pixels(size, (0xE8, 0x11, 0x23))
    assert len(px) == size * size * 4

    def at(x, y):
        i = (y * size + x) * 4
        return tuple(px[i:i + 4])                     # B, G, R, A

    assert at(0, 0)[3] == 0                           # corner: transparent
    assert at(8, 8) == (0x23, 0x11, 0xE8, 255)        # centre: red, opaque
    top = at(8, 0)
    assert top[3] > 0 and min(top[:3]) > 0xE8         # edge: white rim
    assert at(8, 8 - 2)[2] == 0xE8                    # inside the rim: pure red


def test_first_crash_badges_and_flashes():
    alert, b, log, _ = make()
    alert.crashed("a")
    assert b.calls == [("overlay", True), ("flash", True)]
    assert saved() == {"a": "crashed"}
    assert not os.path.exists(ta.STATE_FILE + ".tmp")
    assert alert.names() == ["a"]


def test_same_name_again_is_silent():
    alert, b, _, _ = make()
    alert.crashed("a")
    alert.crashed("a")
    alert.crashed("a", repeat=True)
    assert len(b.calls) == 2


def test_repeat_crash_is_badge_only():
    alert, b, _, _ = make()
    alert.crashed("a", repeat=True)
    assert b.calls == [("overlay", True)]
    b.visible = False
    alert.crashed("b", repeat=True)
    assert b.calls == [("overlay", True)]          # no toast either


def test_gave_up_once_with_long_toast():
    alert, b, _, _ = make()
    alert.crashed("a")
    del b.calls[:]
    alert.gave_up("a")
    assert b.calls == [("overlay", True), ("flash", True), ("toast", True)]
    alert.gave_up("a")
    assert len(b.calls) == 3
    assert saved() == {"a": "gave_up"}
    # a crash report never downgrades a give-up
    alert.crashed("a")
    assert saved() == {"a": "gave_up"} and len(b.calls) == 3


def test_gave_up_hidden_window_still_toasts():
    alert, b, _, _ = make()
    b.visible = False
    alert.gave_up("a")
    assert b.calls == [("toast", True)]


def test_recovered_clears_badge_and_flash():
    alert, b, _, _ = make()
    alert.crashed("a")
    del b.calls[:]
    alert.recovered("a")
    assert b.calls == [("overlay", False), ("flash", False)]
    assert saved() == {} and alert.names() == []
    alert.recovered("a")                              # unknown name: no-op
    alert.recovered("zzz")
    assert len(b.calls) == 2


def test_second_instance_keeps_badge():
    alert, b, _, _ = make()
    alert.crashed("a")
    alert.crashed("b")
    del b.calls[:]
    alert.recovered("a")
    assert b.calls == []
    alert.recovered("b")
    assert b.calls == [("overlay", False), ("flash", False)]


def test_hidden_window_toasts_instead():
    alert, b, _, _ = make()
    b.visible = False
    alert.crashed("a")
    assert b.calls == [("toast", False)]
    del b.calls[:]
    alert.recovered("a")                              # window exists: clear it anyway
    assert b.calls == [("overlay", False), ("flash", False)]


def test_no_window_toasts_and_clears_nothing():
    alert, b, _, _ = make()
    b.hwnd = None
    alert.crashed("a")
    assert b.calls == [("toast", False)]
    alert.recovered("a")
    assert b.calls == [("toast", False)]


def test_tick_reapplies_badge_only_while_alerting():
    alert, b, _, _ = make()
    alert.tick()
    assert b.calls == []
    alert.crashed("a")
    del b.calls[:]
    alert.tick()
    assert b.calls == [("overlay", True)]
    b.visible = False
    alert.tick()
    assert b.calls == [("overlay", True)]             # hidden: nothing to draw on


def test_backend_error_is_swallowed_and_warned_once():
    alert, b, log, _ = make()
    b.fail_overlay = True
    alert.crashed("a")                                # must not raise
    assert b.calls == [("flash", True)]
    assert log.count("warning", "overlay failed") == 1
    alert.tick()                                      # inside the backoff: no retry
    assert b.overlay_attempts == 1
    assert log.count("warning", "overlay failed") == 1


def test_overlay_backoff_retries_and_resets():
    alert, b, log, clock = make()
    b.fail_overlay = True
    alert.crashed("a")
    assert b.overlay_attempts == 1
    clock.now += ta.BACKOFF_MIN - 1
    alert.tick()
    assert b.overlay_attempts == 1
    clock.now += 2
    alert.tick()                                      # 1st retry, fails again
    assert b.overlay_attempts == 2
    clock.now += ta.BACKOFF_MIN + 1                   # second delay is doubled
    alert.tick()
    assert b.overlay_attempts == 2
    b.fail_overlay = False
    clock.now += ta.BACKOFF_MIN * 2
    alert.tick()                                      # 2nd retry succeeds
    assert b.overlay_attempts == 3 and b.calls[-1] == ("overlay", True)
    assert alert._overlay_fail == 0
    alert.tick()                                      # no backoff any more
    assert b.overlay_attempts == 4
    assert log.count("warning", "overlay failed") == 1


def test_disabled_by_settings():
    alert, b, _, _ = make(settings="TaskbarAlert: false\n")
    alert.crashed("a")
    alert.gave_up("a")
    assert b.calls == [] and alert.names() == []
    # switched off while alerting: next tick drops the badge
    alert2, b2, _, _ = make(settings="ManualStopAutoResume: true\n")
    alert2.crashed("a")
    with open(ta.SETTINGS_FILE, "w", encoding="utf-8") as f:
        f.write("TaskbarAlert: false\n")
    del b2.calls[:]
    alert2.tick()
    assert b2.calls == [("overlay", False), ("flash", False)] and alert2.names() == []


def test_unreadable_settings_keep_previous_value():
    alert, b, _, _ = make(settings="TaskbarAlert: false\n")
    alert.crashed("a")
    assert b.calls == []
    with open(ta.SETTINGS_FILE, "w", encoding="utf-8"):
        pass                                          # truncated by set_manual_resume()
    alert.crashed("a")
    assert b.calls == []                              # still off
    os.remove(ta.SETTINGS_FILE)
    alert.crashed("a")
    assert b.calls == [("overlay", True), ("flash", True)]


def test_startup_restores_after_reload():
    alert, b, log, _ = make(reloaded=True, state={"a": "crashed", "gone": "gave_up", "x": "junk"})
    with open("config/a.json", "w") as f:
        f.write("{}")
    alert.startup()
    assert alert.names() == ["a"]
    assert b.calls == [("overlay", True)]             # badge only, no flash, no toast
    assert saved() == {"a": "crashed"}
    assert log.count("info", "restored") == 1


def test_startup_without_reload_clears():
    alert, b, _, _ = make(reloaded=False, state={"a": "crashed"})
    alert.startup()
    assert alert.names() == []
    assert b.calls == [("overlay", False), ("flash", False)]
    assert saved() == {}


def test_startup_disabled_clears_even_after_reload():
    alert, b, _, _ = make(reloaded=True, state={"a": "crashed"}, settings="TaskbarAlert: false\n")
    alert.startup()
    assert alert.names() == [] and b.calls == [("overlay", False), ("flash", False)]


def test_window_lookup_error_is_warned_once_and_falls_back_to_toast():
    alert, b, log, _ = make()

    def boom():
        raise RuntimeError("psutil broke")

    b.find_window = boom
    alert.crashed("a")
    alert.crashed("b")
    assert b.calls == [("toast", False), ("toast", False)]
    assert log.count("warning", "window lookup failed") == 1


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
