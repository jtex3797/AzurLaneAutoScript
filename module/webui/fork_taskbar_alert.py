"""
Fork module: Windows taskbar alert for a scheduler instance that died.

instance_watchdog.py reports crashed()/gave_up()/recovered() from its 60 s
tick in the GUI process. This module turns that into
- a red error badge (ITaskbarList3::SetOverlayIcon) on the taskbar button
  of the Electron window (toolkit/WebApp/alas.exe, title "Alas") plus a
  FlashWindowEx flash that stops once the user activates the window;
- a Windows toast (PowerShell WinRT, no extra package) when that window is
  hidden in the tray or cannot be found (GUI opened in a browser).
The badge stays until the instance runs again (recovered()). A death right
after the watchdog's own restart (repeat=True) only keeps the badge; the
give-up toast is shown even when the window is visible.

Design notes
- Importing this module only captures whether config/reloadalas exists
  (an updater GUI reload). startup(), called once by the watchdog's first
  tick, restores the badge after a reload or clears a stale one.
- find_window()/is_visible() are non-blocking Win32 calls on the caller's
  thread. flash/overlay/toast run on one daemon worker with a per-kind
  "latest wins" slot, so a hung Explorer (COM blocks) never stalls the GUI
  TaskHandler thread, and a dead worker is recreated on the next request.
- Every public method swallows exceptions (TaskHandler drops a task that
  raises). Overlay failures back off exponentially (tick() retries); every
  distinct error is logged once.
- Off switch: `TaskbarAlert: false` in config/fork.yaml (gitignored). Read
  on every call; an unreadable file keeps the previous value because
  instance_watchdog.set_manual_resume() truncates that file while writing.
- Never FindWindowW(None, "Alas"): on this PC it returns Explorer's
  TabProxyWindow of a browser tab with the same title. Windows are matched
  by the PID of alas.exe (an ancestor of the GUI process, else any process
  of that name), top-level (no owner) and the exact title.

Manual check against the real window:
    ./toolkit/python.exe -m module.webui.fork_taskbar_alert --demo
"""
import base64
import json
import os
import subprocess
import threading
import time

import yaml

from module.logger import logger

STATE_FILE = "./log/fork_taskbar_alert.json"
SETTINGS_FILE = "./config/fork.yaml"
SETTINGS_KEY = "TaskbarAlert"
RELOAD_FILE = "./config/reloadalas"
# Captured at import: process_manager removes the file right after the
# reload restarted the instances, long before the watchdog's first tick.
RELOADED = os.path.exists(RELOAD_FILE)

WINDOW_TITLE = "Alas"
WINDOW_EXE = "alas.exe"
# IDI_ERROR (red circle, white X) loaded 16x16 from user32. A path to an
# .ico file here is loaded from that file instead.
OVERLAY_ICON = 32513
OVERLAY_DESC = "오류"
BACKOFF_MIN = 60
BACKOFF_MAX = 1800
TOAST_TIMEOUT = 20
# Registered by the Windows PowerShell start menu shortcut, so the toast is
# shown as coming from "Windows PowerShell".
TOAST_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

TOAST_TITLE = "Alas 봇이 멈췄습니다"
TEXT_CRASHED = "[{name}] 오류로 꺼졌습니다. 10분 뒤 자동 재시작을 시도합니다."
TEXT_GAVE_UP = "[{name}] 자동 재시작 한도를 넘었습니다. 직접 확인이 필요합니다."


class Win32Backend:
    """
    Raw Win32 and COM calls. Every method raises on failure and keeps no
    state besides the window handle cache. All argtypes/restype are set
    explicitly: on 64-bit Python a missing restype truncates handles.
    """

    CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
    IID_TASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

    def __init__(self):
        self._ready = False
        self._hwnd = None

    def _setup(self):
        if self._ready:
            return
        import ctypes
        from ctypes import POINTER, Structure, WINFUNCTYPE
        from ctypes import c_int, c_long, c_ubyte, c_uint, c_ulong, c_ushort, c_void_p, c_wchar_p

        class GUID(Structure):
            _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

        class FLASHWINFO(Structure):
            _fields_ = [("cbSize", c_uint), ("hwnd", c_void_p), ("dwFlags", c_ulong),
                        ("uCount", c_uint), ("dwTimeout", c_ulong)]

        user32, ole32 = ctypes.windll.user32, ctypes.windll.ole32
        HRESULT = c_long  # wintypes of 3.7 has no HRESULT

        def sig(fn, res, *args):
            fn.restype = res
            fn.argtypes = list(args)

        sig(user32.IsWindow, c_int, c_void_p)
        sig(user32.IsWindowVisible, c_int, c_void_p)
        sig(user32.GetWindow, c_void_p, c_void_p, c_uint)
        sig(user32.GetWindowTextW, c_int, c_void_p, c_wchar_p, c_int)
        sig(user32.GetWindowThreadProcessId, c_ulong, c_void_p, POINTER(c_ulong))
        sig(user32.LoadImageW, c_void_p, c_void_p, c_void_p, c_uint, c_int, c_int, c_uint)
        sig(user32.FlashWindowEx, c_int, POINTER(FLASHWINFO))
        enum_proc = WINFUNCTYPE(c_int, c_void_p, c_void_p)
        sig(user32.EnumWindows, c_int, enum_proc, c_void_p)
        sig(ole32.CoInitializeEx, HRESULT, c_void_p, c_uint)
        sig(ole32.CoUninitialize, None)
        sig(ole32.CLSIDFromString, HRESULT, c_wchar_p, POINTER(GUID))
        sig(ole32.CoCreateInstance, HRESULT, POINTER(GUID), c_void_p, c_uint, POINTER(GUID), POINTER(c_void_p))

        self._ct = ctypes
        self._user32, self._ole32 = user32, ole32
        self._GUID, self._FLASHWINFO, self._enum_proc = GUID, FLASHWINFO, enum_proc
        # ITaskbarList3 vtable: 0-2 IUnknown, 3 HrInit, ..., 18 SetOverlayIcon
        self._fn_hr_init = WINFUNCTYPE(HRESULT, c_void_p)
        self._fn_release = WINFUNCTYPE(c_ulong, c_void_p)
        self._fn_set_overlay = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p, c_wchar_p)
        self._ready = True

    @staticmethod
    def _pids():
        """
        Returns:
            set[int]: PIDs of alas.exe, ancestors of this process first (the
                GUI runs under the Electron app), else every such process.
        """
        import psutil
        pids = set()
        for proc in psutil.Process().parents():
            try:
                if proc.name().lower() == WINDOW_EXE:
                    pids.add(proc.pid)
            except psutil.Error:
                pass
        if pids:
            return pids
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() == WINDOW_EXE:
                pids.add(proc.pid)
        return pids

    def find_window(self):
        """
        Returns:
            int | None: Handle of the Alas main window, None if there is none
                (not cached: right after a cold start the title is still empty).
        """
        self._setup()
        ct, user32 = self._ct, self._user32
        if self._hwnd and user32.IsWindow(self._hwnd):
            return self._hwnd
        self._hwnd = None
        pids = self._pids()
        if not pids:
            return None
        found = []
        pid = ct.c_ulong()
        title = ct.create_unicode_buffer(64)

        def callback(hwnd, _):
            user32.GetWindowThreadProcessId(hwnd, ct.byref(pid))
            if pid.value in pids and user32.GetWindow(hwnd, 4) is None:  # GW_OWNER
                user32.GetWindowTextW(hwnd, title, 64)
                if title.value == WINDOW_TITLE:
                    found.append(hwnd)
            return 1

        user32.EnumWindows(self._enum_proc(callback), None)
        self._hwnd = found[0] if found else None
        return self._hwnd

    def is_visible(self, hwnd):
        self._setup()
        return bool(self._user32.IsWindowVisible(hwnd))

    def flash(self, hwnd, on):
        self._setup()
        # FLASHW_TRAY | FLASHW_TIMERNOFG: flash the taskbar button until the
        # window comes to the foreground; FLASHW_STOP otherwise.
        flags = (0x2 | 0xC) if on else 0
        info = self._FLASHWINFO(self._ct.sizeof(self._FLASHWINFO), hwnd, flags, 0, 0)
        self._user32.FlashWindowEx(self._ct.byref(info))

    @staticmethod
    def _check(hr, what):
        hr &= 0xFFFFFFFF
        if hr & 0x80000000:
            raise OSError(f"{what} failed, hr=0x{hr:08X}")

    def _icon(self):
        ct, user32 = self._ct, self._user32
        if isinstance(OVERLAY_ICON, str):
            name = ct.cast(ct.c_wchar_p(OVERLAY_ICON), ct.c_void_p)
            flags = 0x10  # LR_LOADFROMFILE
        else:
            name = ct.c_void_p(OVERLAY_ICON)
            flags = 0x8000  # LR_SHARED
        hicon = user32.LoadImageW(None, name, 1, 16, 16, flags)  # IMAGE_ICON
        if not hicon:
            raise OSError(f"LoadImageW({OVERLAY_ICON!r}) failed")
        return hicon

    def set_overlay(self, hwnd, on):
        self._setup()
        ct, ole32 = self._ct, self._ole32
        # COINIT_APARTMENTTHREADED. S_OK, S_FALSE (already initialised) and
        # RPC_E_CHANGED_MODE (MTA thread) all allow the calls below.
        hr0 = ole32.CoInitializeEx(None, 2) & 0xFFFFFFFF
        if hr0 not in (0, 1, 0x80010106):
            raise OSError(f"CoInitializeEx failed, hr=0x{hr0:08X}")
        try:
            clsid, iid, obj = self._GUID(), self._GUID(), ct.c_void_p()
            self._check(ole32.CLSIDFromString(self.CLSID_TASKBAR_LIST, ct.byref(clsid)), "CLSIDFromString")
            self._check(ole32.CLSIDFromString(self.IID_TASKBAR_LIST3, ct.byref(iid)), "IIDFromString")
            self._check(ole32.CoCreateInstance(ct.byref(clsid), None, 1, ct.byref(iid), ct.byref(obj)),
                        "CoCreateInstance(TaskbarList)")
            vtbl = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p)))[0]
            try:
                self._check(self._fn_hr_init(vtbl[3])(obj), "ITaskbarList3::HrInit")
                hicon = self._icon() if on else None
                self._check(self._fn_set_overlay(vtbl[18])(obj, hwnd, hicon, OVERLAY_DESC if on else None),
                            "ITaskbarList3::SetOverlayIcon")
            finally:
                self._fn_release(vtbl[2])(obj)
        finally:
            if hr0 in (0, 1):
                ole32.CoUninitialize()

    @staticmethod
    def toast(title, body, long=False):
        from xml.sax.saxutils import escape

        def esc(text):
            return escape(text, {'"': "&quot;", "'": "&apos;"})

        duration = ' duration="long"' if long else ""
        xml = (f'<toast{duration}><visual><binding template="ToastGeneric">'
               f"<text>{esc(title)}</text><text>{esc(body)}</text></binding></visual></toast>")
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType=WindowsRuntime] > $null\n"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType=WindowsRuntime] > $null\n"
            "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            f"$x.LoadXml('{xml}')\n"
            "$t = New-Object Windows.UI.Notifications.ToastNotification $x\n"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            f"'{TOAST_AUMID}').Show($t)\n"
        )
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        # -EncodedCommand (UTF-16LE base64) keeps Korean text and quotes
        # intact regardless of the console code page.
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        try:
            code = proc.wait(TOAST_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise OSError("toast timed out")
        if code != 0:
            raise OSError(f"toast exited with {code}")


class TaskbarAlert:
    """
    State machine over the backend. Thread-safe; the watchdog calls it from
    the TaskHandler thread, the worker reports results from its own thread.

    Args:
        backend: Object with find_window/is_visible/flash/set_overlay/toast.
            None = Win32Backend, created lazily. A given backend (tests) is
            called inline on the caller's thread instead of the worker.
    """

    def __init__(self, backend=None):
        self._backend = backend
        self._inline = backend is not None
        self._lock = threading.RLock()
        # name -> "crashed" | "gave_up"
        self._alerts = {}
        self._enabled = True
        self._overlay_fail = 0
        self._overlay_next = 0.0
        self._warned = set()
        # kind -> callable, latest request per kind wins
        self._jobs = {}
        self._cv = threading.Condition()
        self._worker = None

    # ---------- public, never raise ----------

    def startup(self):
        """Once per GUI process: restore the badge after an update reload,
        otherwise clear whatever an earlier GUI left on the window."""
        try:
            with self._lock:
                if RELOADED and self._refresh_enabled():
                    alerts = self._load()
                    self._alerts = {n: s for n, s in alerts.items() if os.path.exists(f"./config/{n}.json")}
                    self._save()
                    if self._alerts:
                        hwnd, visible = self._window()
                        if visible:
                            self._overlay(hwnd, True, force=True)
                    logger.info(f"fork_taskbar_alert: restored after reload {sorted(self._alerts)}")
                else:
                    self.clear_all()
        except Exception as e:
            self._warn(f"startup failed, {e!r}")

    def crashed(self, name, repeat=False):
        """
        Args:
            name (str): Instance name.
            repeat (bool): True when it died right after the watchdog's own
                restart: badge only, no flash, no toast.
        """
        try:
            with self._lock:
                if not self._refresh_enabled() or name in self._alerts:
                    return
                self._alerts[name] = "crashed"
                self._save()
                hwnd, visible = self._window()
                if visible:
                    self._overlay(hwnd, True, force=True)
                    if not repeat:
                        self._flash(hwnd, True)
                elif not repeat:
                    self._toast(TOAST_TITLE, TEXT_CRASHED.format(name=name), False)
                logger.info(f"fork_taskbar_alert: [{name}] crashed, window={'visible' if visible else 'hidden'}, repeat={repeat}")
        except Exception as e:
            self._warn(f"crashed() failed, {e!r}")

    def gave_up(self, name):
        try:
            with self._lock:
                if not self._refresh_enabled() or self._alerts.get(name) == "gave_up":
                    return
                self._alerts[name] = "gave_up"
                self._save()
                hwnd, visible = self._window()
                if visible:
                    self._overlay(hwnd, True, force=True)
                    self._flash(hwnd, True)
                self._toast(TOAST_TITLE, TEXT_GAVE_UP.format(name=name), True)
                logger.info(f"fork_taskbar_alert: [{name}] gave up, window={'visible' if visible else 'hidden'}")
        except Exception as e:
            self._warn(f"gave_up() failed, {e!r}")

    def recovered(self, name):
        try:
            with self._lock:
                if self._alerts.pop(name, None) is None:
                    return
                self._save()
                logger.info(f"fork_taskbar_alert: [{name}] recovered")
                if self._alerts:
                    return
                self._clear_window()
        except Exception as e:
            self._warn(f"recovered() failed, {e!r}")

    def tick(self):
        """Every watchdog tick: re-apply the badge (lost when the window is
        shown again from the tray or Explorer restarted), retry after a
        failure once the backoff is over, drop everything when switched off."""
        try:
            with self._lock:
                if not self._refresh_enabled():
                    if self._alerts:
                        self.clear_all()
                    return
                if not self._alerts:
                    return
                hwnd, visible = self._window()
                if visible:
                    self._overlay(hwnd, True)
        except Exception as e:
            self._warn(f"tick() failed, {e!r}")

    def names(self):
        with self._lock:
            return list(self._alerts)

    def clear_all(self):
        try:
            with self._lock:
                self._alerts = {}
                self._save()
                self._clear_window()
        except Exception as e:
            self._warn(f"clear_all() failed, {e!r}")

    # ---------- internals ----------

    def _backend_get(self):
        if self._backend is None:
            self._backend = Win32Backend()
        return self._backend

    def _window(self):
        """
        Returns:
            tuple[int | None, bool]: (hwnd, visible). Hidden in the tray or
                not found both read as not visible.
        """
        try:
            backend = self._backend_get()
            hwnd = backend.find_window()
            if not hwnd:
                return None, False
            return hwnd, bool(backend.is_visible(hwnd))
        except Exception as e:
            self._warn(f"window lookup failed, {e!r}")
            return None, False

    def _clear_window(self):
        hwnd, _ = self._window()
        if hwnd:
            self._overlay(hwnd, False, force=True)
            self._flash(hwnd, False)

    def _overlay(self, hwnd, on, force=False):
        # State changes always go through; tick() honours the backoff
        if not force and time.monotonic() < self._overlay_next:
            return
        backend = self._backend_get()
        self._submit("overlay", lambda: backend.set_overlay(hwnd, on))

    def _flash(self, hwnd, on):
        backend = self._backend_get()
        self._submit("flash", lambda: backend.flash(hwnd, on))

    def _toast(self, title, body, long):
        backend = self._backend_get()
        self._submit("toast", lambda: backend.toast(title, body, long))

    def _submit(self, kind, job):
        if self._inline:
            self._run(kind, job)
            return
        with self._cv:
            self._jobs[kind] = job
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="fork_taskbar_alert", daemon=True)
                self._worker.start()
            self._cv.notify()

    def _loop(self):
        while True:
            with self._cv:
                while not self._jobs:
                    self._cv.wait()
                kind, job = self._jobs.popitem()
            self._run(kind, job)

    def _run(self, kind, job):
        try:
            job()
        except Exception as e:
            self._on_result(kind, False, e)
        else:
            self._on_result(kind, True, None)

    def _on_result(self, kind, ok, error):
        with self._lock:
            if kind == "overlay":
                if ok:
                    self._overlay_fail = 0
                    self._overlay_next = 0.0
                    return
                self._overlay_fail += 1
                delay = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** (self._overlay_fail - 1))
                self._overlay_next = time.monotonic() + delay
            if not ok:
                self._warn(f"{kind} failed, {error!r}")

    def _warn(self, message):
        with self._lock:
            if message in self._warned:
                return
            self._warned.add(message)
        logger.warning(f"fork_taskbar_alert: {message}")

    def _refresh_enabled(self):
        """
        Returns:
            bool: `TaskbarAlert` of config/fork.yaml, True when the file does
                not exist, the previous value when it cannot be read (it is
                truncated for a moment by set_manual_resume()).
        """
        try:
            if not os.path.exists(SETTINGS_FILE):
                self._enabled = True
            else:
                with open(SETTINGS_FILE, mode="r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict):
                    self._enabled = bool(data.get(SETTINGS_KEY, True))
        except Exception:
            pass
        return self._enabled

    def _save(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, mode="w", encoding="utf-8") as f:
                json.dump(self._alerts, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            self._warn(f"failed to save {STATE_FILE}, {e!r}")

    @staticmethod
    def _load():
        try:
            with open(STATE_FILE, mode="r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): str(v) for k, v in data.items() if v in ("crashed", "gave_up")}
        except Exception:
            return {}


taskbar_alert = TaskbarAlert()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Show the taskbar alert on the real Alas window")
    parser.add_argument("--demo", action="store_true",
                        help="crashed -> 5 s -> gave up -> 5 s -> recovered, calls run inline")
    args = parser.parse_args()
    if args.demo:
        alert = TaskbarAlert()
        alert._inline = True
        alert._save = lambda: None  # leave the GUI's state file alone
        hwnd, visible = alert._window()
        print(f"window: {hwnd}, visible: {visible} (hidden/none -> toast instead of badge)")
        alert.crashed("demo")
        print("crashed: badge + flash, or toast")
        time.sleep(5)
        alert.gave_up("demo")
        print("gave up: badge + flash + long toast")
        time.sleep(5)
        alert.recovered("demo")
        print("recovered: cleared")
    else:
        parser.print_help()
