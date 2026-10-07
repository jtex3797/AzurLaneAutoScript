"""
Fork patch for module/os/map.py, os_auto_search_daemon().

The daemon calls stuck_record_clear() on every frame while is_in_map(), so when the game bugs out with
auto search ON and the fleet standing still, Alas waits forever without a single log line
(upstream issues #4792, #2101; 11 hours on 2026-10-07).
Count time on the map with no battle and no click: toggle auto search off and on once, then raise
GameStuckError so task Restart takes over.
Remove this file and its hook once upstream adds a timeout to os_auto_search_daemon.
"""
import time

from module.exception import GameNotRunningError, GameStuckError
from module.logger import logger
from module.os_handler.assets import AUTO_SEARCH_OS_MAP_OPTION_ON

# Seconds on the map without battle or click. Normal runs stay under 247s, real stalls lasted 383s+
IDLE_LIMIT = 360
# A longer gap between two on-map frames means Alas was off the map (combat, loading, popup)
FRAME_GAP = 15
# A longer gap means a new daemon call or a new task
NEW_RUN_GAP = 30


def auto_search_watch(main):
    """
    Called by OSMap.os_auto_search_daemon() on every frame where is_in_map().

    Args:
        main (OSMap):

    Raises:
        GameStuckError: If still idle after toggling auto search, or auto search option not found.
        GameNotRunningError: If game died.
    """
    state = main.__dict__.setdefault('_fork_as_watch', {
        'last': 0., 'idle': 0., 'count': -1, 'clicks': (), 'toggled': False,
    })
    now = time.time()
    count = main._auto_search_battle_count
    clicks = tuple(main.device.click_record)
    gap = now - state['last']
    state['last'] = now
    if count != state['count'] or gap > NEW_RUN_GAP:
        state.update(count=count, idle=0., toggled=False)
    elif clicks != state['clicks']:
        state['idle'] = 0.
    elif gap < FRAME_GAP:
        state['idle'] += gap
    state['clicks'] = clicks
    if state['idle'] < IDLE_LIMIT:
        return

    idle = int(state['idle'])
    state['idle'] = 0.
    if not state['toggled'] and main.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120)):
        logger.warning(f'Auto search idle on map for {idle}s, toggle auto search off and on')
        state['toggled'] = True
        main.device.click(AUTO_SEARCH_OS_MAP_OPTION_ON)
        # Our own click is not progress
        state['clicks'] = tuple(main.device.click_record)
        return

    logger.warning(f'Auto search idle on map for {idle}s, restart game')
    if main.device.app_is_running():
        # Read by module/fork_recovery.py: an OpSi game bug, not a broken game, so no global rest
        main.device.fork_auto_search_stalled = True
        raise GameStuckError('Auto search idle on map')
    else:
        raise GameNotRunningError('Game died')
