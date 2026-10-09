---
paths:
  - "module/webui/**"
  - "assets/gui/**"
---
# Fork conventions: web GUI (pywebio)

Upstream owns `app.py`, `widgets.py`, `utils.py`, `lang.py`, `alas.css`. The fork adds:

- `module/webui/fork_widgets.py`: `IconSwitchButton(Switch)`, the persistent scheduler START/STOP button pinned to
  the bottom of the aside. Rendered with `put_icon_buttons()` inside `use_scope()`. The rendered column carries a
  `--fork-switch-on--` / `--fork-switch-off--` style marker that `alas-fork.css` colours.
  Right below it, in the same scope and the same Switch (polled state = `(alive, resume on)`, so `app.py` has no
  second hook), sits the on/off toggle of the watchdog's manual-stop resume (`--fork-resume-on--` /
  `--fork-resume-off--`, `toggle_manual_resume()`). The click only flips the state; the next poll redraws.
  Also `workflow_toast_text()` / `show_workflow_status_popup()`: the sync warning toast carries the numbers and
  is clickable, opening a popup with the last run, a link to the workflow page and a 60 s-throttled re-check.
- `module/webui/workflow_checker.py`: asks the GitHub API whether upstream commits are actually missing
  (`compare` with base = this fork's `Branch`, head = `LmeSzinc:AzurLaneAutoScript:master`; `ahead_by` is the
  number that matters), not how long ago `sync-upstream.yml` last ran. GitHub drops scheduled runs under load,
  so an hourly cron really fires every 2-6 h and a run-recency rule cried wolf whenever upstream went quiet.
  States: `ok`, `failure` (latest run failed), `stale` (upstream commits unmerged for over 24 h, or, when the
  compare call is unreachable, last run older than that), `unknown`. `check()` must never raise, because
  `TaskHandler.loop()` permanently drops a task that raises; it also takes a non-blocking lock so the hourly
  tick and the popup's manual re-check cannot interleave and mix up its fields. Registered hourly in `startup()`.
- `module/webui/instance_watchdog.py`: auto-restarts a scheduler instance that died unexpectedly (exit(1)
  paths in `alas.py`) and stayed dead for 10 min. Rolling cap 3 restarts/6 h, then pauses until manual start
  or cooldown. Exit reason = the newest clean-exit sentinel in the last renderables (`Reason: Manual stop`
  etc.), not `ProcessManager.state == 3` alone; skips ticks while `updater.state` is busy.
  Also resumes an instance that was stopped by hand and forgotten: 30 min after the stop AND 10 min without
  keyboard/mouse input on the PC (`GetLastInputInfo`), so it never takes the game from a user at the PC.
  The stop time lives in `log/fork_manual_stop.json` to survive the GUI reload of an update (detected by
  `config/reloadalas` still existing at import); a GUI started by the user clears it, so closing Alas is how
  to keep an instance stopped. On/off lives in `config/fork.yaml` (`ManualStopAutoResume`, gitignored, on by
  default) and is switched by the aside toggle through `set_manual_resume()`.
  Notification: log lines (`*_gui.txt`) plus the taskbar alert below. `check()` must never raise (same
  TaskHandler contract).
  Registered every 60 s in `startup()`. Both automatic starts pass `_emulator_gate()` first: while
  `module/webui/emulator_probe.py` (process list, no adb) says the instance's emulator is off, the start is
  skipped with one warning and retried every tick, plus a 120 s boot settle after it is back (see recovery.md).
- `module/webui/fork_taskbar_alert.py`: taskbar alert for a crashed instance, driven only by the watchdog
  (`startup()` once on its first tick before the updater-busy check, `tick()` every tick, `crashed(name, repeat)`
  on the first sighting of a crash, `gave_up(name)` when the restart budget is spent, `recovered(name)` on
  alive / clean stop / external start that is alive; the import is wrapped in try/except with a no-op stub).
  Red IDI_ERROR overlay (`ITaskbarList3::SetOverlayIcon`, ctypes COM, vtable 3 = HrInit, 18 = SetOverlayIcon,
  all argtypes/restype explicit, hr masked with `& 0xFFFFFFFF`) plus `FlashWindowEx(FLASHW_TRAY|FLASHW_TIMERNOFG)`
  on the Electron window, found by PID of `alas.exe` (ancestor of the GUI process, else any) + no owner + title
  "Alas" through `EnumWindows`. Never `FindWindowW(None, "Alas")`: it returns Explorer's TabProxyWindow of a
  browser tab. PowerShell WinRT toast (`-EncodedCommand`, PowerShell AUMID) when the window is hidden in the tray
  or missing, and always on give-up. The badge stays until `recovered()`; a death right after the watchdog's own
  restart (`repeat=True`) is badge-only. Window lookup runs inline, flash/overlay/toast on one daemon worker with
  a latest-wins slot per kind (a hung Explorer must not stall the TaskHandler thread); overlay failures back off
  60 s doubling to 30 min, each distinct error logged once. The import only captures whether `config/reloadalas`
  exists (`RELOADED`); state lives in `log/fork_taskbar_alert.json` and `startup()` restores it after an update
  reload (instances without `config/<name>.json` dropped) or clears a stale badge otherwise. Off switch:
  `TaskbarAlert: false` in `config/fork.yaml`, read on every call, an unreadable file keeps the previous value
  (`set_manual_resume()` truncates it while writing). Nothing here may raise. Manual check against the real
  window: `./toolkit/python.exe -m module.webui.fork_taskbar_alert --demo`. Tests: `tests/fork/test_taskbar_alert.py`
  (FakeBackend, inline), `tests/fork/test_instance_watchdog_alert.py` (stub module, call order).
- `module/webui/maintenance_checker.py`: official JP maintenance notice in the GUI. Every 15 min `check()` reads
  `azurlane.jp/api/news/list?type=3` (newest row first; `■実施時間` window parsed after NFKC, year from `publishTime`,
  `完了|終了` in the title = finished; a newest row that is live but unparsable gives `unknown`, never last week's
  notice). Every 60 s `observe()` derives the status (`unsupported/unknown/none/scheduled/in_progress/finished`,
  markers `soon/overdue/loading` refine label and colour; finished = title suffix or 2 h past the announced end),
  records transitions in `log/fork_maintenance.json` and sends OnePush on start/extension/end (per JP instance's
  `OnePushConfig`, daemon thread, one push per `status@end`; first observation of an empty state file only records).
  JP = package `com.YoStarJP.AzurLane` or `ServerName` `jp-*`; otherwise nothing is drawn. GUI side in
  `fork_widgets.py`: `MaintenanceAsideLine` (scope `aside_maintenance`, two-line label via `white-space: pre-line`,
  `--fork-maint-<marker>--` colours), `show_maintenance_popup()`, `maintenance_toast()` (session toast, 30 s on
  start/extension, 10 s on end; `toast_plan()` is the pure rule). Nothing here may raise (TaskHandler contract).
  Every 5 min `poll_api()` asks the scheduler's server status API (`sc.shiratama.cn get_state`, the JP instance's
  server name) only from 10 min before the announced start until 2 h after the announced end: "down, then up
  with a newer last_update" pins the real end to the minute (`finished_at`, label "점검 종료 19:54", finish push
  with the time), "down again" is an extension (new `api_round`, so the push keys `status@end@round` do not
  block the second finish). The HTTP call runs outside the lock; the answer is dropped if the notice changed.
  The API fields are saved with the notice's (id, end) and restored across restarts. Unparsable notice = no
  polling. Probe: `./toolkit/python.exe -m module.webui.maintenance_checker --once [--api]`. Tests: `tests/fork/`, run with
  `./toolkit/python.exe -m tests.fork.test_maintenance_checker` (no pytest needed; chdir's to a temp dir after import).
- `assets/gui/css/alas-fork.css`: all fork CSS. Loaded in `AlasGUI.run()` right after `alas` via
  `add_css(filepath_css("alas-fork"))`. Net fork diff in `alas.css` is zero; keep it that way.
- Hooks inside `app.py` (the only fork edits there): `put_scope("aside_scheduler_btn")` plus the immediate redraw in
  `set_aside()`, creation of `aside_scheduler_switch` guarded by `hasattr`, the `workflow_notify` toast switch,
  the `instance_watchdog` import + `task_handler.add(instance_watchdog.check, 60)` in `startup()`, and
  the `{"label": "한국어", "value": "ko-KR"}` language option. Maintenance notice: the `MaintenanceAsideLine`/`maintenance_toast` import,
  `put_scope("aside_maintenance")` + redraw in `set_aside()`, `aside_maintenance_line` creation in `show()`,
  the `maintenance_switch` toast Switch in `run()`, and `maintenance_checker.check/observe/poll_api` in `startup()`.

Rules
- `alas-fork.css` must stay ASCII-only, comments included. `add_css()` in `module/webui/utils.py` opens the file
  without an encoding, so on this PC (cp949) a single non-ASCII byte crashes GUI start with UnicodeDecodeError.
  This already happened once (typographic dash in a comment).
- A Switch whose scope is wiped by `clear=True` has to reset its generator and call `.switch()` to redraw at once;
  see the comment in `set_aside()`.
- GUI-side errors go to `log/<date>_gui.txt`, not to the scheduler log. `dev_tools/fork_log_triage.py` reads both.
- Find the fork's exact lines in an upstream file with
  `git log --first-parent --no-merges -p 92c07aa28..HEAD -- <file>`.
- Check a GUI change end to end without touching the live app: `git archive HEAD gui.py alas.py module deploy
  config submodule assets/gui campaign` into a scratch dir, overlay the changed files, and run
  `<repo>/toolkit/python.exe gui.py --port <free port> --host 127.0.0.1` there (`module.logger` chdir's into
  that copy, so its `config/` and `log/` are its own). Drive headless Edge over `--remote-debugging-port` with
  the bundled `websockets` (CDP `Runtime.evaluate` reads the DOM and clicks). A plain `--screenshot` fires
  before pywebio has drawn anything.
