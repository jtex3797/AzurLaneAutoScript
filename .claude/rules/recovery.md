---
paths:
  - "alas.py"
  - "module/fork_recovery.py"
  - "module/webui/process_manager.py"
  - "module/webui/instance_watchdog.py"
---
# Fork conventions: recover instead of stopping

Three layers keep the bot running without a human. Upstream `alas.py` is untouched.

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
2. GUI process, crash path of `module/webui/instance_watchdog.py`: see `.claude/rules/webui.md`.
3. GUI process, manual-stop resume in the same file.

Left out on purpose
- No emulator restart. With an `emulator-*` serial, `Device.__init__` never raises `EmulatorNotRunningError`,
  `emulator_start_watch()` waits for the `127.0.0.1:*` serial that adb never lists, and the BlueStacks5 stop
  pattern expects quoted arguments that psutil does not return. Needs a run against the real emulator first.
- No handler for the defeat pages inside auto search (upstream combat code).

Testing
- `alas.py` `loop()`/`run()` can be driven for real with a fake config/device/checker injected as properties
  on a subclass; `exit(1)` surfaces as `SystemExit`.
- The watchdog can be loaded from a file with stub `module.webui.process_manager` / `module.webui.updater`
  entries in `sys.modules`, a fake clock on its `time` name and a patched `idle_seconds`.
- Such scripts must follow hard rule 4 in `CLAUDE.md`.
