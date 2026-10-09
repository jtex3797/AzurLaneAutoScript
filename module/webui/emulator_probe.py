import os

from deploy.Windows.utils import iter_process
from module.config.deep import deep_get
from module.config.utils import filepath_config, read_file
from module.logger import logger

"""
Fork module: is the emulator of an Alas instance running right now?

Used by instance_watchdog as a gate in front of its automatic starts, so a
scheduler is not started against an emulator that is not there (with an
`emulator-*` serial Alas never starts the emulator itself and would die on
the first screenshot, burning the restart budget).

Judged from the process list only (deploy.Windows.utils.iter_process, psutil):
the executable name from `Alas.EmulatorInfo.path` and, for BlueStacks 5, the
`--instance <name>` argument. adb is not used on purpose: an `emulator-5554`
entry is only picked up when the adb server starts, so "not listed" does not
mean "not running", and starting the adb server can block for seconds.
Keep this module free of module.device / module.webui imports: it is
imported by instance_watchdog and must stay light.
"""

# name -> (config mtime, Alas.EmulatorInfo dict or None)
_cache = {}


def _emulator_info(name):
    """
    Args:
        name (str): Alas instance name.

    Returns:
        dict | None: `Alas.EmulatorInfo` of config/<name>.json, None when the
            file or the key is missing. Re-read only when the file changed.
    """
    try:
        path = filepath_config(name)
        mtime = os.path.getmtime(path)
    except Exception:
        return None
    cached = _cache.get(name)
    if cached is None or cached[0] != mtime:
        try:
            data = read_file(path)
        except Exception:
            return None
        info = deep_get(data, "Alas.EmulatorInfo", None)
        _cache[name] = (mtime, info if isinstance(info, dict) else None)
    return _cache[name][1]


def _target(info):
    """
    Returns:
        tuple[str, str] | None: (executable name lower-cased, instance name)
    """
    if not info or not info.get("path"):
        return None
    exe = os.path.basename(str(info["path"]).replace("\\", "/")).lower()
    if exe == "hd-multiinstancemanager.exe":
        # Same treatment as module/device/platform/emulator_windows.py
        exe = "hd-player.exe"
    return exe, str(info.get("name") or "")


def _matches(proc_info, exe, inst):
    """
    Args:
        proc_info: DataProcessInfo from iter_process()
        exe (str), inst (str): from _target()

    Returns:
        bool: this process is the wanted emulator instance
    """
    if proc_info.name.lower() != exe:
        return False
    try:
        argv = list(proc_info.proc.cmdline())
    except Exception:
        # AccessDenied and the like: arguments unknown, so treat the running
        # player as ours (unknown must never look like "off")
        argv = []
    if "--instance" not in argv:
        # A player started without --instance (default shortcut)
        return True
    i = argv.index("--instance")
    return not inst or (i + 1 < len(argv) and argv[i + 1] == inst)


def emulator_running(name):
    """
    Args:
        name (str): Alas instance name.

    Returns:
        bool | None: True if a matching emulator process exists, False if the
            process list was scanned and none matched, None when it cannot be
            judged (no config, no EmulatorInfo path, psutil missing, errors).
    """
    target = _target(_emulator_info(name))
    if target is None:
        return None
    exe, inst = target
    seen = 0
    try:
        for proc_info in iter_process():
            seen += 1
            if _matches(proc_info, exe, inst):
                return True
    except Exception as e:
        logger.warning(f"emulator_probe: process scan failed, {e!r}")
        return None
    # iter_process() yields nothing at all when psutil is missing
    return False if seen else None


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Print whether the emulator of an Alas instance is running")
    parser.add_argument("name", nargs="?", default="alas", help="instance name, default alas")
    args = parser.parse_args()
    info = _emulator_info(args.name)
    print("EmulatorInfo:", info)
    target = _target(info)
    print("target:", target)
    start = time.perf_counter()
    result = emulator_running(args.name)
    print(f"emulator_running({args.name!r}) = {result} in {time.perf_counter() - start:.3f}s")
    if target:
        exe, inst = target
        for p in iter_process():
            if p.name.lower() == exe:
                try:
                    argv = list(p.proc.cmdline())
                except Exception as e:
                    argv = f"<{e!r}>"
                print(f"  candidate pid={p.pid} name={p.name} argv={argv} match={_matches(p, exe, inst)}")
