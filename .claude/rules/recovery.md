---
paths:
  - "alas.py"
  - "module/fork_recovery.py"
  - "module/webui/process_manager.py"
  - "module/webui/instance_watchdog.py"
  - "module/os/map.py"
  - "module/os/fork_auto_search_watch.py"
---
# Fork conventions: recover instead of stopping

Four layers keep the bot running without a human. Upstream `alas.py` is untouched.

1. Scheduler process, `module/fork_recovery.py` (`ForkAzurLaneAutoScript`, subclass of `alas.AzurLaneAutoScript`).
   Hooked by one added import line in `ProcessManager.run_process()`: scheduler loop only, single functions and
   `python alas.py` still run the upstream class.
   - `GamePageUnknownError`: upstream calls `exit(1)` inside its `except` block. The subclass catches the
     `SystemExit` whose `__context__` is that error, queues task `Restart` and returns False. Upstream has
     already saved the dump and sent its "crashed" push by then.
   - Same task failing 3 times: `handle_task_failure()` runs before `loop()` counts the failure, postpones the
     task (30/60/120/240 min, back to 30 after a success) and sets `failure_record[task] = -1` so `loop()`
     lands on 0 instead of exiting. Task `Restart` is left alone and still exits.
   - Logs "fleet lost the battle" when the last screenshot matches `OPTS_INFO_D` / `BATTLE_STATUS_D`. Auto
     search has no handler for the defeat pages, so a lost battle shows up as `GameStuckError`.
     A battle still running (any PAUSE skin, `Combat.is_combat_executing()`) counts too: PAUSE is a 180s long
     wait button, so a weak fleet in a 3+ min battle also ends as `GameStuckError` (Main 15-2, 2026-10-05).
     A postpone round with such a failure uses 30/60/360 min instead (user's choice: rest 6 h after 1 h).
   - 3 different tasks postponed within 120 min = game or emulator broken, not one task: `get_next_task()`
     closes the game and waits 180 min (`rest()`, config edits from GUI don't end it), then queues `Restart`.
     One task failing forever never triggers it (its own ladder handles that), nor do battle-lost rounds (a
     weak fleet) or OpSi stall rounds (layer 4 sets `device.fork_auto_search_stalled` before raising); the
     game still works for other tasks. In memory: a scheduler restart (GUI update) ends the rest early.
   - Error dumps `log/error/<ms>` older than 14 days are removed at scheduler start and after each new dump.
2. GUI process, crash path of `module/webui/instance_watchdog.py`: see `.claude/rules/webui.md`. It also drives
   the taskbar alert (`module/webui/fork_taskbar_alert.py`, red badge + flash on the Alas window, toast when
   hidden) on crash / give-up / recovery; described there too.
3. GUI process, manual-stop resume in the same file.
4. OpSi auto search stall, `module/os/fork_auto_search_watch.py`, hooked by one import and one call in
   `OSMap.os_auto_search_daemon()` right after its per-frame `stuck_record_clear()`. That clear means a map with
   auto search ON and a fleet standing still never trips the 60s/180s stuck timers (upstream #4792, #2101).
   After 360s on the map with no battle and no click it clicks `AUTO_SEARCH_OS_MAP_OPTION_ON` once (upstream
   turns it back on), then raises `GameStuckError` (or `GameNotRunningError`) after another 360s, so layer 1
   applies. Normal runs stay under 247s idle on the map; recorded stalls were 383s to 11h.

Left out on purpose
- No emulator restart. With an `emulator-*` serial, `Device.__init__` never raises `EmulatorNotRunningError`,
  `emulator_start_watch()` waits for the `127.0.0.1:*` serial that adb never lists, and the BlueStacks5 stop
  pattern expects quoted arguments that psutil does not return. Needs a run against the real emulator first.
  What exists instead (2026-10-09): `module/webui/emulator_probe.py` judges from the process list whether the
  instance's emulator is running (`Alas.EmulatorInfo.path` executable name, BlueStacks5 `--instance <name>`
  argument; adb is not used because an `emulator-*` entry is only picked up when the adb server starts), and
  `instance_watchdog._emulator_gate()` skips the crash restart and the manual-stop resume while it says off,
  then waits `BOOT_SETTLE_SECONDS` (120 s) after it is back. Budget, grace and the 30 min count are untouched,
  so the start happens on the next tick. Unknown verdicts (no psutil, no EmulatorInfo) keep the old behaviour.
- No handler for the defeat pages inside auto search (upstream combat code).
- No "emulator window not focused" rule for the manual-stop resume (tried, then removed). The user often runs
  auto battles by hand while working in other windows, so an unfocused emulator does not mean nobody plays.

Manual-stop resume, known limits (PC input is the only signal)
- Auto battle started by hand, then the user steps away or only watches a video: no input for 10 min, so Alas
  takes the game over 30 min after the stop.
- The same account played on a phone while the PC sits idle: the resume logs in and kicks the phone session.
- No resume while the PC is used without a 10 min break, or when the idle time can't be read.
- The answer to all three is the aside toggle: switch the resume off before playing by hand or on the phone.

Testing
- `alas.py` `loop()`/`run()` can be driven for real with a fake config/device/checker injected as properties
  on a subclass; `exit(1)` surfaces as `SystemExit`.
- The watchdog can be loaded from a file with stub `module.webui.process_manager` / `module.webui.updater`
  entries in `sys.modules` (plus a stub `module.webui.fork_taskbar_alert`), a fake clock on its `time` name and
  a patched `idle_seconds`. Import `module.logger` AND `module.webui` before the chdir to the scratch directory:
  the package pulls in `deploy/logger.py`, which chdir's to the repo root again on its first import, so a test
  that chdir's before it silently writes its state files into the live `log/` (found 2026-10-09).
- Such scripts must follow hard rule 4 in `CLAUDE.md`.
