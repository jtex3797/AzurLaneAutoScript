---
paths:
  - "module/config/**"
  - "config/template.json"
---
# Config and i18n generation

Pipeline (full diagram in the `if __name__ == '__main__':` docstring at the bottom of `module/config/config_updater.py`):

    task.yaml + argument.yaml + override.yaml + gui.yaml
        -> argument/args.json, argument/menu.json
        -> config_generated.py, i18n/<lang>.json, config/template.json

- Hand-edit ONLY: `argument/argument.yaml`, `task.yaml`, `default.yaml`, `override.yaml`, `gui.yaml`,
  `config_manual.py`.
- Never hand-edit generated files: `argument/args.json`, `argument/menu.json`, `config_generated.py`,
  `config/template.json`.
- Regenerate with `./toolkit/python.exe -m module.config.config_updater`. It rewrites every `i18n/<lang>.json`,
  adding new keys and keeping existing translations.
- User settings live in `config/<name>.json` (gitignored). `config/deploy.yaml` is gitignored too.

Korean (fork)
- `LANGUAGES` in `module/config/utils.py` contains `'ko-KR'`. It is a one-word edit on an upstream line, so expect a
  merge conflict there if upstream ever adds a language.
- `module/config/i18n/ko-KR.json` is fork-owned. Upstream never touches it, so new upstream options arrive without
  Korean text. The fork patch in `module/webui/lang.py` (`_t()` and `reload()`) falls back to en-US when a key is
  missing or its value still equals the key.
- To translate new options: run the generator, then `git diff module/config/i18n/ko-KR.json` and translate only the
  added values. The i18n files are 100 KB+ each: grep the key, never read them whole.
