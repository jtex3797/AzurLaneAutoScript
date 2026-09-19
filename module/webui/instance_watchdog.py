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
UPDATER_BUSY_STATES = ("start", "wait", "run update", "reload")
SENTINEL_SCAN = 5
SENTINELS = ("Reason: Manual stop", "Reason: Finish", "Reason: Update")


class InstanceWatchdog:
    """
    Fork module: auto-restart watchdog for crashed scheduler instances.

    Watches every ProcessManager instance that was started at least once in
    this GUI session. When an instance has been dead without a clean-exit
    sentinel for GRACE_SECONDS, it is restarted the same way the manual
    start button does (pm.start(None, updater.event)), at most RESTART_LIMIT
    times per rolling RESTART_WINDOW_SECONDS. Notification is log-only.

    check() must never raise: TaskHandler.loop() permanently removes a task
    that raises, which would silently kill this watchdog until GUI restart.

    Known limitations:
    - Pressing Stop on an already-dead instance writes no "Manual stop"
      sentinel (ProcessManager.stop() only writes it while alive), so such
      an instance still looks crashed and gets revived once; stopping the
      revived instance works normally.
    - Watchdog memory (restart budget, gave-up flag) lives in this GUI
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

        if rec["proc_ref"]() is not proc:
            # A start this watchdog did not perform (it updates proc_ref
            # right after its own start): manual intervention, so reset
            # the budget and re-arm. weakref instead of id() because a
            # freed Process object's id can be reused by its successor.
            rec["proc_ref"] = weakref.ref(proc)
            rec["state3_since"] = None
            rec["restarts"] = []
            rec["gave_up"] = False
            logger.info(
                f"instance_watchdog: [{pm.config_name}] started externally, watchdog re-armed"
            )
            return

        if pm.alive:
            rec["state3_since"] = None
            return
        if not self._is_crashed(pm):
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
    def _is_crashed(pm):
        """
        Dead without a clean-exit sentinel in the last few renderables.
        Not pm.state == 3 alone: stop() appends its sentinel while the log
        drain thread may still append child lines after it, so a manually
        stopped instance can transiently look like state 3.
        """
        tail = pm.renderables[-SENTINEL_SCAN:]
        if not tail:
            return False
        console = Console(no_color=True)
        for r in tail:
            if isinstance(r, str):
                s = r
            else:
                with console.capture() as capture:
                    console.print(r)
                s = capture.get()
            for sentinel in SENTINELS:
                if sentinel in s:
                    return False
        return True


instance_watchdog = InstanceWatchdog()
