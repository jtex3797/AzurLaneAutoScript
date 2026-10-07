from datetime import datetime, timedelta

import inflection

import alas
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

        failed = self.failure_record.get(task, 0) + 1
        if failed < FAILURE_LIMIT:
            return
        if task == 'Restart':
            # Postponing the restart of a game that can't start helps nothing, let loop() exit
            return

        battle_lost = task in self.battle_lost_record
        self.battle_lost_record.discard(task)
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
