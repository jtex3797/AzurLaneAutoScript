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
- `module/webui/workflow_checker.py`: asks the GitHub API for the latest run of the fork's `sync-upstream.yml`.
  States: `ok`, `failure`, `stale` (no run for 6 h), `unknown`. `check()` must never raise, because
  `TaskHandler.loop()` permanently drops a task that raises. Registered hourly in `startup()`.
- `assets/gui/css/alas-fork.css`: all fork CSS. Loaded in `AlasGUI.run()` right after `alas` via
  `add_css(filepath_css("alas-fork"))`. Net fork diff in `alas.css` is zero; keep it that way.
- Hooks inside `app.py` (the only fork edits there): `put_scope("aside_scheduler_btn")` plus the immediate redraw in
  `set_aside()`, creation of `aside_scheduler_switch` guarded by `hasattr`, the `workflow_notify` toast switch, and
  the `{"label": "한국어", "value": "ko-KR"}` language option.

Rules
- `alas-fork.css` must stay ASCII-only, comments included. `add_css()` in `module/webui/utils.py` opens the file
  without an encoding, so on this PC (cp949) a single non-ASCII byte crashes GUI start with UnicodeDecodeError.
  This already happened once (typographic dash in a comment).
- A Switch whose scope is wiped by `clear=True` has to reset its generator and call `.switch()` to redraw at once;
  see the comment in `set_aside()`.
- GUI-side errors go to `log/<date>_gui.txt`, not to the scheduler log. `dev_tools/fork_log_triage.py` reads both.
- Find the fork's exact lines in an upstream file with
  `git log --first-parent --no-merges -p 92c07aa28..HEAD -- <file>`.
