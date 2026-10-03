import ctypes
import json
import os
import threading
import time
import weakref

from rich.console import Console

from module.logger import logger
from module.webui.process_manager import ProcessManager
from module.webui.updater import updater

GRACE_SECONDS = 600
RESTART_LIMIT = 3
RESTART_WINDOW_SECONDS = 6 * 3600
# A manually stopped instance is started again once the stop is this old
# and the PC had no keyboard or mouse input for MANUAL_IDLE_SECONDS.
# Set MANUAL_RESUME_SECONDS to None to disable.
MANUAL_RESUME_SECONDS = 1800
MANUAL_IDLE_SECONDS = 600
MANUAL_STOP_FILE = "./log/fork_manual_stop.json"
# Written by the updater right before it reloads the GUI
RELOAD_FILE = "./config/reloadalas"
UPDATER_BUSY_STATES = ("start", "wait", "run update", "reload")
SENTINEL_SCAN = 5
SENTINEL_MANUAL = "Reason: Manual stop"
SENTINELS = (SENTINEL_MANUAL, "Reason: Finish", "Reason: Update")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """
    Returns:
        float: Seconds since the last keyboard or mouse input on this PC,
            None if unknown (not Windows, GUI not in the user's session).
    """
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        if info.dwTime == 0:
            # No input ever seen, a session without a user
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        # Both are 32-bit millisecond counters that wrap around after 49.7 days
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000
    except Exception:
        return None


class InstanceWatchdog:
    """
    Fork module: auto-restart watchdog for scheduler instances.

    Crashed: watches every ProcessManager instance that was started at least
    once in this GUI session. When an instance has been dead without a
    clean-exit sentinel for GRACE_SECONDS, it is restarted the same way the
    manual start button does (pm.start(None, updater.event)), at most
    RESTART_LIMIT times per rolling RESTART_WINDOW_SECONDS.

    Stopped by hand and forgotten: when the newest sentinel is "Manual stop",
    the instance is started again once MANUAL_RESUME_SECONDS have passed and
    the PC has been idle for MANUAL_IDLE_SECONDS, so it never grabs the game
    from a user who is still at the PC. Not counted in the crash budget.
    The stop time is kept in MANUAL_STOP_FILE to survive the GUI reload of
    an update; a GUI started by the user forgets it, so closing Alas is the
    way to keep an instance stopped.

    Notification is log-only. check() must never raise: TaskHandler.loop()
    permanently removes a task that raises, which would silently kill this
    watchdog until GUI restart.

    Known limitations:
    - Pressing Stop on an already-dead instance writes no "Manual stop"
      sentinel (ProcessManager.stop() only writes it while alive), so such
      an instance still looks crashed and gets revived once; stopping the
      revived instance works normally.
    - The updater force-stopping an instance after its 10 min wait writes
      the same sentinel, so that looks like a manual stop too.
    - A PC in use never counts as idle, so there is no resume while the
      user keeps working on it. A PC waking from sleep without input can
      resume at once, sleep time counts as idle time.
    - Crash memory (restart budget, gave-up flag) lives in this GUI
      process and resets on GUI reload / auto-update restart.
    - Upstream ProcessManager.start() is an unlocked check-then-act, so a
      manual Start click in the same instant as an auto restart can in
      theory double-start (same pre-existing race as two browser tabs
      clicking Start at once); the alive re-check under the per-config
      lock narrows this to milliseconds.
    """

    def __init__(self):
        # config_name -> mutable record dict, see _record()
        self._tracked = {}
        # config_name -> time.time() the manual stop was first seen
        self._manual = {}
        if os.path.exists(RELOAD_FILE):
            self._manual = self._manual_load()
        else:
            self._manual_save()

    def check(self):
        # Registered in startup() via task_handler.add(self.check, 60).
        try:
            # Busy check by updater.state only: updater.event can stay set
            # forever after a no-reload update success, which would mute
            # the watchdog permanently if used alone.
            if updater.state in UPDATER_BUSY_STATES:
                return
            now = time.monotonic()
            for pm in list(ProcessManager._processes.values()):
                try:
                    self._check_instance(pm, now)
                except Exception as e:
                    logger.warning(
                        f"instance_watchdog: [{getattr(pm, 'config_name', '?')}] {e!r}"
                    )
            # Manual stops carried over a GUI reload, nothing started them
            # in this GUI session so _check_instance() skips them.
            for name in list(self._manual):
                try:
                    self._check_carried(name)
                except Exception as e:
                    logger.warning(f"instance_watchdog: [{name}] {e!r}")
        except Exception as e:
            logger.warning(f"instance_watchdog: {e!r}")

    def _check_instance(self, pm, now):
        proc = pm._process
        if proc is None:
            # Never started in this GUI session; not ours to manage.
            return
        rec = self._tracked.get(pm.config_name)
        if rec is None:
            rec = self._record(proc)
            self._tracked[pm.config_name] = rec
            # Started in this GUI session, a carried over stop is history
            self._manual_forget(pm.config_name)

        if rec["proc_ref"]() is not proc:
            # A start this watchdog did not perform (it updates proc_ref
            # right after its own start): manual intervention, so reset
            # the budget and re-arm. weakref instead of id() because a
            # freed Process object's id can be reused by its successor.
            rec["proc_ref"] = weakref.ref(proc)
            rec["state3_since"] = None
            rec["restarts"] = []
            rec["gave_up"] = False
            # Also when it was started and stopped again between two ticks
            self._manual_forget(pm.config_name)
            logger.info(
                f"instance_watchdog: [{pm.config_name}] started externally, watchdog re-armed"
            )
            return

        if pm.alive:
            rec["state3_since"] = None
            self._manual_forget(pm.config_name)
            return
        sentinel = self._last_sentinel(pm)
        if sentinel == SENTINEL_MANUAL:
            rec["state3_since"] = None
            self._check_manual_stop(pm)
            return
        self._manual_forget(pm.config_name)
        if sentinel != "":
            rec["state3_since"] = None
            return

        if rec["gave_up"]:
            self._prune(rec, now)
            if not rec["restarts"]:
                rec["gave_up"] = False
                rec["state3_since"] = None
                logger.info(
                    f"instance_watchdog: [{pm.config_name}] cooldown over, watchdog re-armed"
                )
            return
        if rec["state3_since"] is None:
            rec["state3_since"] = now
            logger.warning(
                f"instance_watchdog: [{pm.config_name}] scheduler died unexpectedly, "
                f"auto restart in {GRACE_SECONDS}s if it stays down"
            )
            return
        if now - rec["state3_since"] < GRACE_SECONDS:
            return
        self._prune(rec, now)
        if len(rec["restarts"]) >= RESTART_LIMIT:
            rec["gave_up"] = True
            logger.error(
                f"instance_watchdog: [{pm.config_name}] auto restart limit reached "
                f"({RESTART_LIMIT} per {RESTART_WINDOW_SECONDS // 3600}h), pausing until "
                f"manual start or cooldown"
            )
            return
        self._restart(pm, rec, now)

    def _restart(self, pm, rec, now):
        # Same per-config lock stop() uses, so we never race a Stop click.
        lock = pm._process_locks.setdefault(pm.config_name, threading.Lock())
        with lock:
            # Re-check just before acting: an update or a manual start may
            # have begun since the top of this tick.
            if updater.state in UPDATER_BUSY_STATES:
                return
            if pm.alive:
                return
            rec["restarts"].append(now)
            rec["state3_since"] = None
            logger.warning(
                f"instance_watchdog: [{pm.config_name}] auto restarting "
                f"({len(rec['restarts'])}/{RESTART_LIMIT} in rolling "
                f"{RESTART_WINDOW_SECONDS // 3600}h)"
            )
            # Same call as the manual start button. Passing updater.event
            # lets the child exit by itself if an update starts later.
            pm.start(None, updater.event)
            rec["proc_ref"] = weakref.ref(pm._process)

    def _check_carried(self, name):
        pm = ProcessManager.get_manager(name)
        if pm._process is not None:
            # Handled by _check_instance()
            return
        if not os.path.exists(f"./config/{name}.json"):
            self._manual_forget(name)
            return
        self._check_manual_stop(pm)

    def _check_manual_stop(self, pm):
        if MANUAL_RESUME_SECONDS is None:
            return
        name = pm.config_name
        now = time.time()
        since = self._manual.get(name)
        if since is None or since > now:
            self._manual[name] = now
            self._manual_save()
            logger.info(
                f"instance_watchdog: [{name}] stopped manually, auto resume after "
                f"{MANUAL_RESUME_SECONDS}s once the PC is idle for {MANUAL_IDLE_SECONDS}s"
            )
            return
        if now - since < MANUAL_RESUME_SECONDS:
            return
        idle = idle_seconds()
        if idle is None or idle < MANUAL_IDLE_SECONDS:
            return
        self._resume(pm)

    def _resume(self, pm):
        # Same per-config lock stop() uses, so we never race a Stop click.
        lock = pm._process_locks.setdefault(pm.config_name, threading.Lock())
        with lock:
            # Re-check just before acting, like _restart()
            if updater.state in UPDATER_BUSY_STATES:
                return
            if pm.alive:
                return
            if pm._process is not None and self._last_sentinel(pm) != SENTINEL_MANUAL:
                return
            logger.warning(
                f"instance_watchdog: [{pm.config_name}] auto resuming after manual stop"
            )
            pm.start(None, updater.event)
            self._manual_forget(pm.config_name)
            rec = self._tracked.get(pm.config_name)
            if rec is not None:
                # Our own start, keep the crash budget as it is
                rec["proc_ref"] = weakref.ref(pm._process)
                rec["state3_since"] = None

    def _manual_forget(self, name):
        if self._manual.pop(name, None) is not None:
            self._manual_save()

    def _manual_save(self):
        try:
            with open(MANUAL_STOP_FILE, mode="w", encoding="utf-8") as f:
                json.dump(self._manual, f)
        except Exception as e:
            logger.warning(f"instance_watchdog: failed to save manual stops, {e!r}")

    @staticmethod
    def _manual_load():
        """
        Returns:
            dict: config_name -> time.time() of the manual stop
        """
        try:
            with open(MANUAL_STOP_FILE, mode="r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): float(v) for k, v in data.items()}
        except Exception:
            return {}

    @staticmethod
    def _record(proc):
        return {
            "proc_ref": weakref.ref(proc),
            "state3_since": None,  # monotonic time the crash was first seen
            "restarts": [],  # monotonic times of auto restarts, pruned to window
            "gave_up": False,
        }

    @staticmethod
    def _prune(rec, now):
        rec["restarts"] = [
            t for t in rec["restarts"] if now - t < RESTART_WINDOW_SECONDS
        ]

    @staticmethod
    def _last_sentinel(pm):
        """
        Newest clean-exit sentinel in the last few renderables.
        Not pm.state alone: stop() appends its sentinel while the log
        drain thread may still append child lines after it, so a manually
        stopped instance can transiently look like state 3.

        Returns:
            str: One of SENTINELS, "" if there is none (dead means crashed),
                None if the instance has no output at all.
        """
        tail = pm.renderables[-SENTINEL_SCAN:]
        if not tail:
            return None
        console = Console(no_color=True)
        for r in reversed(tail):
            if isinstance(r, str):
                s = r
            else:
                with console.capture() as capture:
                    console.print(r)
                s = capture.get()
            for sentinel in SENTINELS:
                if sentinel in s:
                    return sentinel
        return ""


instance_watchdog = InstanceWatchdog()
