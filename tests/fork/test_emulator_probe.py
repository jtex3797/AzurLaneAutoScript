"""
Unit tests of module/webui/emulator_probe.py (fork). No real processes are
scanned: iter_process() and _emulator_info() are replaced with fakes.

Run from the repository root, no pytest needed:
    ./toolkit/python.exe -m tests.fork.test_emulator_probe
"""
import os
import sys
import tempfile
import traceback

import module.webui.emulator_probe as probe

SCRATCH = tempfile.mkdtemp(prefix="alas_fork_probe_test_")
os.chdir(SCRATCH)

INFO = {"Emulator": "BlueStacks5", "name": "Pie64", "path": "C:/Program Files/BlueStacks_nxt/HD-Player.exe"}


class FakeProc:
    def __init__(self, argv, fail=False):
        self._argv = argv
        self._fail = fail

    def cmdline(self):
        if self._fail:
            raise PermissionError("AccessDenied")
        return list(self._argv)


class FakeInfo:
    def __init__(self, name, argv, fail=False):
        self.name = name
        self.pid = 1
        self.proc = FakeProc(argv, fail)


def run(procs, info=INFO):
    probe.iter_process = lambda: iter(procs)
    probe._emulator_info = lambda name: info
    return probe.emulator_running("fake")


def test_matching_instance_is_running():
    procs = [FakeInfo("svchost.exe", ["svchost.exe"]),
             FakeInfo("HD-Player.exe", ["C:\\BlueStacks_nxt\\HD-Player.exe", "--instance", "Pie64"])]
    assert run(procs) is True


def test_other_instance_is_not_ours():
    procs = [FakeInfo("HD-Player.exe", ["HD-Player.exe", "--instance", "Nougat32"])]
    assert run(procs) is False


def test_player_without_instance_argument_counts():
    procs = [FakeInfo("hd-player.exe", ["HD-Player.exe"])]
    assert run(procs) is True


def test_services_only_means_off():
    procs = [FakeInfo("BstkSVC.exe", ["BstkSVC.exe"]), FakeInfo("BlueStacksServices.exe", ["x"]),
             FakeInfo("", [])]
    assert run(procs) is False


def test_empty_scan_means_unknown():
    assert run([]) is None


def test_missing_path_means_unknown():
    assert run([FakeInfo("HD-Player.exe", ["HD-Player.exe"])], info={"Emulator": "auto"}) is None
    assert run([FakeInfo("HD-Player.exe", ["HD-Player.exe"])], info=None) is None


def test_cmdline_access_denied_counts_as_running():
    procs = [FakeInfo("HD-Player.exe", ["HD-Player.exe", "--instance", "Nougat32"], fail=True)]
    assert run(procs) is True


def test_multi_instance_manager_maps_to_player():
    info = dict(INFO, path="C:/Program Files/BlueStacks_nxt/HD-MultiInstanceManager.exe")
    procs = [FakeInfo("HD-Player.exe", ["HD-Player.exe", "--instance", "Pie64"])]
    assert run(procs, info=info) is True


def test_scan_exception_means_unknown():
    def boom():
        raise RuntimeError("psutil broke")
        yield  # noqa: unreachable, makes this a generator

    probe.iter_process = boom
    probe._emulator_info = lambda name: INFO
    assert probe.emulator_running("fake") is None


if __name__ == "__main__":
    failures = 0
    names = [n for n in list(globals()) if n.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(names) - failures}/{len(names)} passed, scratch {SCRATCH}")
    sys.exit(1 if failures else 0)
