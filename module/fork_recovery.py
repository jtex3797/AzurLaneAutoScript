import os
import shutil
import time
from datetime import datetime, timedelta

import inflection

import alas
from module.base.decorator import del_cached_property
from module.config.deep import deep_get
from module.exception import GamePageUnknownError
from module.logger import logger
from module.notify import handle_notify

# alas.py loop() stops the scheduler when one task fails this many times in a row
FAILURE_LIMIT = 3
# Delay of a task that keeps failing, one step further on each repeat, back to the first after a success
POSTPONE_MINUTES = (30, 60, 120, 240)
# Same, when the fleet lost a battle in that round: the fleet stays too weak, so rest long after 1 hour
BATTLE_LOST_POSTPONE_MINUTES = (30, 60, 360)
# This many different tasks postponed within the window means the game or emulator is broken, not one task.
# Close the game and run nothing for REST_MINUTES.
BROKEN_TASK_COUNT = 3
BROKEN_WINDOW_MINUTES = 120
REST_MINUTES = 180
# Error dumps in ./log/error/<ms> older than this are removed
ERROR_KEEP_DAYS = 14


class ForkAzurLaneAutoScript(alas.AzurLaneAutoScript):
    """
    Fork module: recover by restarting instead of stopping the scheduler.

    Used by ProcessManager.run_process() for the scheduler loop only, single
    functions (daemons, tools) still run on the upstream class.

    - GamePageUnknownError: upstream exits, here the game is restarted,
      so a game left on an unsupported page no longer needs a human.
      Upstream has already sent its "crashed" notification by then.
    - A task failing FAILURE_LIMIT times in a row: upstream exits, here the
      task is postponed and the other tasks keep running.
    - BROKEN_TASK_COUNT different tasks postponed within BROKEN_WINDOW_MINUTES:
      the game is closed and all tasks rest for REST_MINUTES.
    - Error dumps older than ERROR_KEEP_DAYS are removed.

    Task `Restart` failing FAILURE_LIMIT times in a row (the game can't even
    reach the main page), ScriptError, RequestHumanTakeover and unexpected
    exceptions still exit, module/webui/instance_watchdog.py revives the
    process after those.

    No emulator restart here: with an `emulator-*` serial upstream's
    emulator_start() waits for the `127.0.0.1:*` serial that never shows up
    in adb, and its BlueStacks5 stop pattern expects quoted arguments, so it
    kills nothing. Needs a test against the real emulator before adding.
    """

    def __init__(self, config_name='alas'):
        super().__init__(config_name=config_name)
        # Key: str, task name, value: int, how many times the task was postponed since its last success
        self.postpone_record = {}
        # Tasks that lost a battle since their last postpone or success
        self.battle_lost_record = set()
        # Tasks that hit the OpSi auto search stall since their last postpone or success
        self.stalled_record = set()
        # (datetime, task) of each postpone within BROKEN_WINDOW_MINUTES
        self.postpone_history = []
        # datetime, run nothing until then
        self.rest_until = None
        self.clean_error_log()

    def get_next_task(self):
        if self.rest_until is not None:
            rest_until, self.rest_until = self.rest_until, None
            if datetime.now() < rest_until:
                self.rest(rest_until)
        return super().get_next_task()

    def rest(self, future):
        """
        Close the game and run nothing until `future`, then restart the game.

        Args:
            future (datetime):
        """
        logger.warning(f'Too many tasks failing, close game and rest until {future}')
        try:
            from module.base.resource import release_resources
            self.device.app_stop()
            release_resources()
            self.device.release_during_wait()
        except Exception as e:
            logger.warning(f'Failed to close game before rest: {e}')
        # wait_until() returns False when the config is changed from GUI, keep resting
        while not self.wait_until(future):
            del_cached_property(self, 'config')
        logger.info('Rest finished, restart game')
        self.config.task_call('Restart')
        del_cached_property(self, 'config')

    def run(self, command, skip_first_screenshot=False):
        try:
            success = super().run(command, skip_first_screenshot=skip_first_screenshot)
        except SystemExit as e:
            # exit(1) is called inside the except block of alas.py run(),
            # so the exception it was handling is chained as __context__
            if not isinstance(e.__context__, GamePageUnknownError):
                raise
            logger.warning(f'Game page unknown, {self.device.package} will be restarted in 10 seconds')
            logger.warning('If you are playing by hand, please stop Alas')
            self.config.task_call('Restart')
            self.device.sleep(10)
            success = False

        # goto_main during wait also calls run(), it is not a task of loop()
        task = self.config.task.command
        if inflection.underscore(task) == command:
            if success:
                self.postpone_record.pop(task, None)
                self.battle_lost_record.discard(task)
                self.stalled_record.discard(task)
            else:
                self.handle_task_failure(task)
        return success

    def handle_task_failure(self, task):
        """
        Called before loop() counts the failure, to take over when loop() is about to exit.

        Args:
            task (str): Task name, such as `Main`
        """
        if self.is_defeat_page():
            logger.warning('Last screenshot is the battle defeat page, fleet lost the battle')
            self.battle_lost_record.add(task)
        elif self.is_battle_running():
            # PAUSE is a long wait button, so a battle lasting over 3 minutes ends up as GameStuckError
            logger.warning('Last screenshot is a battle still running after 3 minutes, fleet is too weak')
            self.battle_lost_record.add(task)
        if self.pop_auto_search_stalled():
            self.stalled_record.add(task)

        failed = self.failure_record.get(task, 0) + 1
        if failed < FAILURE_LIMIT:
            return
        if task == 'Restart':
            # Postponing the restart of a game that can't start helps nothing, let loop() exit
            return

        battle_lost = task in self.battle_lost_record
        self.battle_lost_record.discard(task)
        stalled = task in self.stalled_record
        self.stalled_record.discard(task)
        ladder = BATTLE_LOST_POSTPONE_MINUTES if battle_lost else POSTPONE_MINUTES
        count = self.postpone_record.get(task, 0)
        self.postpone_record[task] = count + 1
        minute = ladder[min(count, len(ladder) - 1)]
        logger.warning(f'Task `{task}` failed {FAILURE_LIMIT} times in a row, '
                       f'postpone it for {minute} minutes and keep running other tasks')
        # Don't pull the task earlier if it has delayed itself further
        next_run = deep_get(self.config.data, keys=f'{task}.Scheduler.NextRun', default=None)
        if next_run is None or next_run < datetime.now() + timedelta(minutes=minute):
            self.config.task_delay(minute=minute, task=task)
        reason = 'fleet lost the battle' if battle_lost else 'see log for the reason'
        handle_notify(
            self.config.Error_OnePushConfig,
            title=f"Alas <{self.config_name}> task postponed",
            content=f"<{self.config_name}> Task `{task}` failed {FAILURE_LIMIT} times in a row ({reason}), "
                    f"postponed for {minute} minutes",
        )

        # loop() adds this failure on top, landing on 0 instead of FAILURE_LIMIT
        self.failure_record[task] = -1
        # Lost battles mean a weak fleet and an OpSi stall is a game bug there, neither is a broken game,
        # other tasks can still run
        if not battle_lost and not stalled:
            self.check_broken(task)

    def check_broken(self, task):
        """
        Start a rest if BROKEN_TASK_COUNT different tasks were postponed within BROKEN_WINDOW_MINUTES.

        Args:
            task (str): Task just postponed
        """
        now = datetime.now()
        window = timedelta(minutes=BROKEN_WINDOW_MINUTES)
        self.postpone_history = [(t, k) for t, k in self.postpone_history if now - t < window]
        self.postpone_history.append((now, task))
        tasks = sorted(set(k for _, k in self.postpone_history))
        if len(tasks) < BROKEN_TASK_COUNT:
            return

        self.postpone_history = []
        self.rest_until = now + timedelta(minutes=REST_MINUTES)
        logger.warning(f'Tasks {tasks} were all postponed within {BROKEN_WINDOW_MINUTES} minutes, '
                       f'game or emulator may be broken, rest {REST_MINUTES} minutes after this task')
        handle_notify(
            self.config.Error_OnePushConfig,
            title=f"Alas <{self.config_name}> resting",
            content=f"<{self.config_name}> Tasks {tasks} kept failing, "
                    f"game closed and all tasks rest for {REST_MINUTES} minutes",
        )

    def save_error_log(self):
        super().save_error_log()
        self.clean_error_log()

    def clean_error_log(self):
        """
        Remove error dumps ./log/error/<ms> older than ERROR_KEEP_DAYS.
        """
        try:
            folder = './log/error'
            if not os.path.isdir(folder):
                return
            limit = (time.time() - ERROR_KEEP_DAYS * 86400) * 1000
            removed = 0
            for name in os.listdir(folder):
                if name.isdigit() and int(name) < limit:
                    shutil.rmtree(os.path.join(folder, name), ignore_errors=True)
                    removed += 1
            if removed:
                logger.info(f'Removed {removed} error logs older than {ERROR_KEEP_DAYS} days')
        except Exception as e:
            logger.warning(f'Failed to clean error logs: {e}')

    def is_defeat_page(self):
        """
        Returns:
            bool: If the last screenshot is a page shown after losing a battle, the D rank
                or the "strengthen your fleet" tips. Auto search does not handle them,
                so a lost battle ends up as GameStuckError.
        """
        try:
            # Assets must be imported after config is loaded, they are server specific
            from module.combat.assets import BATTLE_STATUS_D, OPTS_INFO_D
            image = self.device.image
            return bool(OPTS_INFO_D.match(image, offset=(30, 30)) or BATTLE_STATUS_D.match(image, offset=(30, 30)))
        except Exception:
            return False

    def pop_auto_search_stalled(self):
        """
        Returns:
            bool: If module/os/fork_auto_search_watch.py raised this failure. Resets the flag.
        """
        try:
            stalled = getattr(self.device, 'fork_auto_search_stalled', False)
            self.device.fork_auto_search_stalled = False
            return bool(stalled)
        except Exception:
            return False

    def is_battle_running(self):
        """
        Returns:
            bool: If the last screenshot is a battle in progress, a PAUSE button of any skin.
        """
        try:
            from module.combat.combat import Combat
            return bool(Combat(self.config, device=self.device).is_combat_executing())
        except Exception:
            return False
