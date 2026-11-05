import json
import time
from datetime import datetime, timedelta, timezone

from module.logger import logger
from module.server_checker import ServerChecker

"""
Fork module: log in by hand-made probes near the end of a JP maintenance.

The upstream scheduler only waits on the server status API (module/server_checker.py,
up to 10 min between polls). Here, while that API reports maintenance:
- From PROBE_LEAD before the announced end, the game is started and a login is tried
  every PROBE_INTERVAL. A patch download offered on the title screen is accepted on the way.
- The API is also polled every WINDOW_API_INTERVAL inside that window.
- Past the announced end it keeps trying until PROBE_STALE, so a late opening is caught.
- The announced end is read on every step from the GUI's notice state file
  (module/webui/maintenance_checker.py), so a corrected or extended notice moves the window.
- A login that reaches page_main counts as open: the checker turns available and recovered,
  and loop() queues Restart as it does after an API recovery.

Hooked by ForkAzurLaneAutoScript.checker (module/fork_recovery.py), scheduler loop only.
Nothing here may raise out of wait_until_available(): loop() calls it without a try.
"""

JST = timezone(timedelta(hours=9))
# Written by the GUI process (module/webui/maintenance_checker.py STATE_FILE), relative to
# the repo root that module.logger chdir'ed into
STATE_FILE = './log/fork_maintenance.json'
PROBE_LEAD = timedelta(minutes=30)
PROBE_INTERVAL = timedelta(minutes=10)
# Past the announced end by this much, the notice is an old one: stop probing
PROBE_STALE = timedelta(hours=12)
# After a probe saw the server open, ignore the first "maintenance" answer of the API within this
PROBE_TRUST = timedelta(minutes=30)
# ... and every other one within this much after that first one (one task failure)
TRUST_GRACE = timedelta(seconds=60)
WINDOW_API_INTERVAL = 120
WAIT_STEP = 10
PROBE_TIMEOUT = 480
# After a probe clicked the patch download, later probes don't stop the game for this long
DOWNLOAD_KEEP = timedelta(minutes=30)
JP_PACKAGE = 'com.YoStarJP.AzurLane'


class ProbeTimeout(Exception):
    pass


def read_notice_end(path):
    """
    Returns:
        datetime | None: announced end of the newest maintenance notice, aware.
            None if the file is missing, unreadable, half written or has no end.
    """
    try:
        with open(path, mode='r', encoding='utf-8') as f:
            data = json.load(f)
        end = datetime.fromisoformat(data['end'])
        if end.tzinfo is None:
            return None
        return end
    except Exception:
        return None


def in_window(now, end):
    """
    Args:
        now (datetime): aware
        end (datetime | None): announced end

    Returns:
        bool: inside [end - PROBE_LEAD, end + PROBE_STALE]
    """
    if end is None:
        return False
    return end - PROBE_LEAD <= now <= end + PROBE_STALE


def probe_due(now, end, last_probe):
    """
    Returns:
        bool: a login probe should run now
    """
    if not in_window(now, end):
        return False
    return last_probe is None or now - last_probe >= PROBE_INTERVAL


class ForkServerChecker(ServerChecker):
    def __init__(self, server, probe):
        """
        Args:
            server (str): Emulator_ServerName
            probe (callable): returns True if a login reached page_main. Must not raise,
                exceptions are caught anyway.
        """
        # Set before super().__init__(), which calls check_now()
        self.probe = probe
        self.last_probe = None
        self.probe_open_at = None
        self.trust_until = None
        super().__init__(server)

    @staticmethod
    def _now():
        return datetime.now(JST)

    def wait_until_available(self):
        while not self.is_available():
            try:
                if self._probe_step():
                    return
            except Exception as e:
                logger.warning(f'Maintenance probe step failed: {e!r}')
            time.sleep(WAIT_STEP)

    def _probe_step(self):
        """
        Returns:
            bool: True if a probe saw the server open
        """
        end = read_notice_end(STATE_FILE)
        now = self._now()
        if in_window(now, end) and self._timer.limit > WINDOW_API_INTERVAL:
            self._timer.limit = WINDOW_API_INTERVAL
            logger.info(f'Maintenance ends at {end.strftime("%H:%M")}, '
                        f'server checker will retry every {WINDOW_API_INTERVAL}s')
        if not probe_due(now, end, self.last_probe):
            return False
        self.last_probe = now
        logger.info(f'Maintenance probe: announced end {end.strftime("%m-%d %H:%M")}, try to log in')
        if self.probe():
            self._mark_open(now)
            return True
        logger.info(f'Maintenance probe: still closed, next try at '
                    f'{(now + PROBE_INTERVAL).strftime("%H:%M")}')
        return False

    def _mark_open(self, now):
        logger.info('Server is open, seen by maintenance probe')
        self.probe_open_at = now
        self._state.append(True)
        self._recover = True
        self._timer.limit = 0
        self._timer.reset()

    def check_now(self):
        super().check_now()
        if not self._state or self._state[-1]:
            return
        now = self._now()
        if self.probe_open_at is not None:
            # The first "maintenance" answer after an open probe starts a short grace,
            # one failure calls check_now() twice (alas.py run() and loop())
            if now - self.probe_open_at <= PROBE_TRUST:
                self.trust_until = now + TRUST_GRACE
            self.probe_open_at = None
        if self.trust_until is None:
            return
        if now > self.trust_until:
            # Past the grace a real re-maintenance is followed, the probes judge it again
            self.trust_until = None
            return
        logger.info('Server checker still reports maintenance, trust the login probe')
        self._state[-1] = True
        self._timer.limit = 0
        self._timer.reset()


def run_probe(config, device, keep_running=False):
    """
    Start the game and try to log in.

    Args:
        config (AzurLaneConfig):
        device (Device):
        keep_running (bool): don't stop the game first, a patch download may be running

    Returns:
        tuple[bool, bool]: (reached page_main, patch download clicked)
    """
    # Assets are server specific and fixed at import, import after config is loaded
    from module.exception import GameNotRunningError, GameStuckError, GameTooManyClickError
    from module.handler.login import LoginHandler

    class ProbeLogin(LoginHandler):
        download_clicked = False

        def appear_then_click(self, button, *args, **kwargs):
            clicked = super().appear_then_click(button, *args, **kwargs)
            if clicked and str(button) == 'LOGIN_GAME_UPDATE':
                logger.info('Maintenance probe: game patch download started')
                self.download_clicked = True
            return clicked

        def confirm_main(self):
            # handle_app_login() also returns after a page_main popup, not only on page_main
            hit = 0
            for _ in range(10):
                self.device.sleep(1)
                self.device.screenshot()
                hit = hit + 1 if self.is_in_main() else 0
                if hit >= 3:
                    return True
            return False

    login = ProbeLogin(config, device=device)
    deadline = time.time() + PROBE_TIMEOUT
    original = device.screenshot

    def screenshot():
        if time.time() > deadline:
            raise ProbeTimeout(f'Login probe took over {PROBE_TIMEOUT}s')
        return original()

    opened = False
    device.screenshot = screenshot
    try:
        if not keep_running:
            device.app_stop()
        device.app_start()
        login.handle_app_login()
        opened = login.confirm_main()
    except (GameStuckError, GameTooManyClickError, GameNotRunningError, ProbeTimeout) as e:
        logger.info(f'Maintenance probe: login failed, {type(e).__name__}: {e}')
    finally:
        try:
            del device.screenshot
        except AttributeError:
            pass
        device.stuck_record_clear()
        device.click_record_clear()
    return opened, login.download_clicked
