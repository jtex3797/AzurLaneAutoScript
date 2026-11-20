"""
Fork patch for OSMap.os_auto_search_daemon() in module/os/map.py, installed at runtime, no line in map.py.

Upstream "Fix: detect stalled OpSi auto search" (2026-11-14) lets the stuck timer run while auto search is ON and
the walk counter above the fleet stays the same, so a fleet standing still raises GameStuckError after 60s and
upstream restarts the game (before that fix Alas waited forever, upstream issues #4792, #2101).
Here the stall detection stays upstream's and only the reaction changes:
- First stall in a daemon call: toggle auto search off and run the daemon again (it turns auto search back on).
- Second stall in the same call: mark it for module/fork_recovery.py, which keeps an OpSi game bug out of the
  3-hour rest, then raise so task Restart takes over.

Installed by ForkAzurLaneAutoScript.run() for OpSi tasks, so the scheduler loop only. GUI single functions and
`python alas.py` run upstream's class and get upstream's behaviour.
Remove this file and its call in module/fork_recovery.py once upstream retries auto search before restarting.
"""
import functools

from module.exception import CampaignEnd, GameStuckError
from module.logger import logger
from module.os_handler.assets import AUTO_SEARCH_OS_MAP_OPTION_ON


def is_auto_search_map(main):
    """
    Args:
        main (OSMap):

    Returns:
        bool: If the last screenshot is an OpSi map with auto search ON. GameStuckError is raised before
            a new screenshot is taken, so this is the frame the fleet stalled on.
    """
    try:
        return bool(main.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120)))
    except Exception:
        return False


def retry_daemon(original):
    """
    Args:
        original: OSMap.os_auto_search_daemon from upstream.

    Returns:
        Wrapped os_auto_search_daemon.
    """

    @functools.wraps(original)
    def daemon(self, drop=None, strategic=False):
        try:
            return original(self, drop=drop, strategic=strategic)
        except GameStuckError:
            if not is_auto_search_map(self):
                raise
            # The daemon resets the counter when it starts again
            finished = self._auto_search_battle_count
            logger.warning('Auto search stalled on map, toggle auto search off and run it again')
            self.device.click(AUTO_SEARCH_OS_MAP_OPTION_ON)

        try:
            return finished + original(self, drop=drop, strategic=strategic)
        except CampaignEnd:
            # os_auto_search_run() reads the counter after CampaignEnd
            self._auto_search_battle_count += finished
            raise
        except GameStuckError:
            if is_auto_search_map(self):
                logger.warning('Auto search stalled on map again, restart game')
                # Read by module/fork_recovery.py: an OpSi game bug, not a broken game, so no global rest
                self.device.fork_auto_search_stalled = True
            raise

    daemon._fork_retry = True
    return daemon


def install():
    """
    Wrap OSMap.os_auto_search_daemon once per process.
    """
    from module.os.map import OSMap
    original = getattr(OSMap, 'os_auto_search_daemon', None)
    if original is None:
        logger.warning('OSMap.os_auto_search_daemon not found, auto search retry not installed')
        return
    if getattr(original, '_fork_retry', False):
        return
    OSMap.os_auto_search_daemon = retry_daemon(original)
