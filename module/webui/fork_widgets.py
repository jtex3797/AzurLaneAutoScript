from datetime import datetime

from pywebio.output import popup, put_button, put_link, put_scope, put_text, toast, use_scope
from module.logger import logger
from module.webui.instance_watchdog import MANUAL_IDLE_SECONDS, MANUAL_RESUME_SECONDS, instance_watchdog
from module.webui.maintenance_checker import maintenance_checker, toast_plan, window_text
from module.webui.utils import Switch
from module.webui.widgets import put_icon_buttons
from module.webui.workflow_checker import workflow_checker

# Circled icons, drawn in the same wrapper as assets/gui/icon/*.svg so the
# existing `.aside-icon` sizing applies. The ring shape keeps them visually
# distinct from the plain outlined nav icons (the instance button uses a bare
# play triangle); per-state colors come from alas-fork.css.
_RING = (
    '<path fill-rule="evenodd" d="M512 64a448 448 0 1 1 0 896a448 448 0 1 1'
    ' 0-896z m0 64a384 384 0 1 0 0 768a384 384 0 1 0 0-768z"></path>'
)
ICON_START = (
    '<svg class="aside-icon icon-start" viewBox="0 0 1024 1024" version="1.1"'
    ' xmlns="http://www.w3.org/2000/svg">' + _RING +
    '<path d="M416 320L720 512L416 704z"></path></svg>'
)
ICON_STOP = (
    '<svg class="aside-icon icon-stop" viewBox="0 0 1024 1024" version="1.1"'
    ' xmlns="http://www.w3.org/2000/svg">' + _RING +
    '<path d="M368 368h288v288H368z"></path></svg>'
)
# Clock hands: the resume happens by itself after a while
ICON_RESUME = (
    '<svg class="aside-icon icon-resume" viewBox="0 0 1024 1024" version="1.1"'
    ' xmlns="http://www.w3.org/2000/svg">' + _RING +
    '<path d="M480 304h64v176h144v64H480z"></path></svg>'
)

WORKFLOW_POPUP_SCOPE = "fork_workflow_status"


class IconSwitchButton(Switch):
    """
    Same two-state behaviour as widgets.BinarySwitchButton, but rendered like
    the other aside entries (icon above, small label below) via
    put_icon_buttons(), which has no scope argument and must therefore be
    wrapped in use_scope().

    The rendered column carries a `--fork-switch-on--` / `--fork-switch-off--`
    style marker (same trick as put_icon_buttons' `--aside-{value}--`) so CSS
    can color each state.

    Right below it, in the same scope, sits the toggle of instance_watchdog's
    resume after a manual stop (`--fork-resume-on--` / `--fork-resume-off--`).
    Both are driven by this one Switch, whose polled state is the pair
    (button state, resume on), so app.py needs no second hook and a click on
    the toggle shows up in every open browser session.
    """

    def __init__(
            self,
            get_state,
            label_on,
            label_off,
            icon_on,
            icon_off,
            onclick_on,
            onclick_off,
            scope,
            value="fork:scheduler",
    ):
        self.scope = scope
        # ':' cannot appear in an alas instance name, so this marker can never
        # collide with the `--aside-{value}--` selector of a real instance
        # button in active_button().
        self.value = value
        self.button = {
            False: (label_off, icon_off, onclick_off, "off"),
            True: (label_on, icon_on, onclick_on, "on"),
        }
        super().__init__(
            status=self.update_button,
            get_state=lambda: (bool(get_state()), instance_watchdog.manual_resume),
            name=scope,
        )

    def update_button(self, state):
        if state == -1:
            # Nothing changed since the last poll
            return
        on, resume = state
        label, icon, onclick, marker = self.button[on]
        with use_scope(self.scope, clear=True):
            put_icon_buttons(
                icon,
                buttons=[{"label": label, "value": self.value, "color": "aside"}],
                onclick=[onclick],
            ).style(f"--fork-switch-{marker}--")
            put_icon_buttons(
                ICON_RESUME,
                buttons=[{
                    "label": "자동재개" if resume else "재개꺼짐",
                    "value": "fork:resume",
                    "color": "aside",
                }],
                onclick=[toggle_manual_resume],
            ).style(f"--fork-resume-{'on' if resume else 'off'}--")


def toggle_manual_resume():
    """
    Click on the resume toggle. No redraw here: the next poll of every
    IconSwitchButton sees the changed state and redraws its scope within a
    second, which also avoids two threads rendering the same scope at once.
    """
    enabled = not instance_watchdog.manual_resume
    instance_watchdog.set_manual_resume(enabled)
    if enabled:
        toast(
            f"수동 정지 후 자동 재개 켜짐 — 정지 {MANUAL_RESUME_SECONDS // 60}분 뒤, "
            f"PC 입력이 {MANUAL_IDLE_SECONDS // 60}분 없으면 다시 시작합니다",
            duration=5,
            position="right",
            color="success",
        )
    else:
        toast(
            "수동 정지 후 자동 재개 꺼짐 — 직접 시작할 때까지 멈춰 있습니다",
            duration=5,
            position="right",
            color="warn",
        )


def _hours(seconds):
    return f"{seconds / 3600:.0f}시간"


def _local(time):
    """
    UTC datetime from the GitHub API -> local wall clock, '-' when unknown.
    """
    if time is None:
        return "-"
    return time.astimezone().strftime("%m-%d %H:%M")


def workflow_toast_text(state):
    """
    Text of the sticky sync warning. It carries the numbers so the toast alone
    already says how bad it is, and show_workflow_status_popup() has the rest.
    """
    if state == "failure":
        return "업스트림 동기화 실패 — 눌러서 확인"
    behind = workflow_checker.behind
    lag = workflow_checker.lag_seconds
    if behind and lag:
        return f"업스트림 동기화가 밀려 있음 ({behind}개 커밋 / {_hours(lag)} 지연) — 눌러서 확인"
    if behind:
        return f"업스트림 동기화가 밀려 있음 ({behind}개 커밋) — 눌러서 확인"
    return "업스트림 동기화가 멈춰 있음 — 눌러서 확인"


def workflow_headline(state, behind, lag):
    """
    One line saying what the sync is doing, for the top of the status popup.
    """
    if state == "ok" and behind:
        return f"정상 — {behind}개 커밋 대기 중, 다음 실행이 가져옵니다"
    if state == "ok":
        return "정상 — 빠진 업스트림 커밋 없음"
    if state == "failure":
        return "자동 병합 실패 — GitHub Actions에서 원인을 확인하세요"
    if state == "stale":
        lag_text = f" / {_hours(lag)} 지연" if lag else ""
        return f"밀려 있음 — {behind if behind else '?'}개 커밋{lag_text}"
    return "확인 불가 — 네트워크 또는 저장소 설정 문제"


def show_workflow_status_popup():
    """
    Opened by clicking the sync warning toast, which on its own can only say
    that something is wrong and leaves no way to look into it.
    """
    popup("업스트림 동기화 상태", [put_scope(WORKFLOW_POPUP_SCOPE)])
    _put_workflow_status()


def _put_workflow_status():
    # Snapshot first: the hourly check runs in another thread and would
    # otherwise change these fields halfway through rendering.
    state = workflow_checker.state
    behind = workflow_checker.behind
    lag = workflow_checker.lag_seconds
    run_at = workflow_checker.run_at
    conclusion = workflow_checker.run_conclusion
    checked_at = workflow_checker.checked_at
    url = workflow_checker.workflow_url

    with use_scope(WORKFLOW_POPUP_SCOPE, clear=True):
        put_text(workflow_headline(state, behind, lag))
        put_text(f"마지막 자동 실행: {_local(run_at)} ({conclusion or '-'})")
        put_text(f"마지막 점검: {_local(checked_at)}")
        if url:
            put_link("GitHub Actions 열기", url=url, new_window=True)
        put_button("지금 다시 확인", onclick=_recheck, small=True)


def _recheck():
    # Synchronous on purpose: two API calls, 15s timeout each in the worst
    # case. recheck() is throttled, so a stuck click cannot be repeated into
    # a pile of blocked requests.
    with use_scope(WORKFLOW_POPUP_SCOPE, clear=True):
        put_text("확인 중...")
    if not workflow_checker.recheck():
        toast("방금 확인했습니다 — 잠시 후 다시 시도하세요", duration=3, position="right", color="warn")
    _put_workflow_status()


# ---------------------------------------------------------------------------
# Maintenance notice line, popup and toasts (module/webui/maintenance_checker.py)

MAINT_POPUP_SCOPE = "fork_maintenance_status"
# Wrench in the same ring as the other fork icons
_WRENCH = (
    'M700 286a150 150 0 0 0-196 180L300 670l54 54 204-204a150 150 0 0 0'
    ' 180-196l-84 84-72-14-14-72z'
)
ICON_MAINT = (
    '<svg class="aside-icon icon-maint" viewBox="0 0 1024 1024" version="1.1"'
    ' xmlns="http://www.w3.org/2000/svg">' + _RING +
    '<path d="' + _WRENCH + '"></path></svg>'
)
# Past the announced end without a completion notice: a double ring (outer
# r448/48 + inner r352/40 in one evenodd path) around a smaller wrench, so
# it differs from the plain in-progress ring in shape, not only in colour.
# The wrench sits in a <g transform>, so alas-fork.css colours this icon
# with a descendant selector (`.aside-icon path`), not `> path`.
_RING_DOUBLE = (
    '<path fill-rule="evenodd" d="M512 64a448 448 0 1 1 0 896a448 448 0 1 1'
    ' 0-896z m0 48a400 400 0 1 0 0 800a400 400 0 1 0 0-800z'
    ' M512 160a352 352 0 1 1 0 704a352 352 0 1 1 0-704z'
    ' m0 40a312 312 0 1 0 0 624a312 312 0 1 0 0-624z"></path>'
)
ICON_MAINT_OVERDUE = (
    '<svg class="aside-icon icon-maint-overdue" viewBox="0 0 1024 1024" version="1.1"'
    ' xmlns="http://www.w3.org/2000/svg">' + _RING_DOUBLE +
    '<g transform="translate(128 128) scale(0.75)"><path d="' + _WRENCH + '"></path></g></svg>'
)


class MaintenanceAsideLine(Switch):
    """
    Aside entry right below the scheduler button: "점검 중 / ~20:00 대기" and
    the like, polled from maintenance_checker.snapshot(). Clicking it opens
    show_maintenance_popup(). Nothing is drawn for a non-JP setup.

    update() must never raise: the session TaskHandler drops a task that
    raises and show() creates this Switch only once, so the line would stay
    frozen until the page is reloaded.
    """

    def __init__(self, scope):
        self.scope = scope
        super().__init__(
            status=self.update,
            get_state=maintenance_checker.snapshot,
            name=scope,
        )

    def update(self, state):
        if state == -1:
            return
        try:
            status, label, marker = state
            with use_scope(self.scope, clear=True):
                if status == "unsupported":
                    return
                put_icon_buttons(
                    ICON_MAINT_OVERDUE if marker == "overdue" else ICON_MAINT,
                    buttons=[{"label": label, "value": "fork:maint", "color": "aside"}],
                    onclick=[show_maintenance_popup],
                ).style(f"--fork-maint-{marker}--")
        except Exception as e:
            logger.warning(f"maintenance aside line failed, {e!r}")


def maintenance_headline(status, marker):
    if status == "unsupported":
        return "일본 서버 인스턴스가 없어 점검 공지를 확인하지 않습니다"
    if status == "unknown":
        if marker == "loading":
            return "공지를 아직 확인하지 못했습니다 (GUI 시작 직후)"
        return "공지를 가져오지 못했습니다"
    if status == "none":
        return "예정된 점검 공지가 없습니다"
    if status == "scheduled":
        if marker == "soon":
            return "1시간 안에 점검이 시작됩니다. 시작되면 봇이 게임을 끄고 기다립니다"
        return "점검이 예정되어 있습니다. 시작되면 봇이 게임을 끄고 기다립니다"
    if status == "in_progress":
        if marker == "overdue":
            return "예정 종료 시각이 지났지만 완료 공지가 아직 없습니다. 조금 더 기다립니다"
        return "게임을 끄고 점검이 끝나기를 기다리는 중입니다. 봇을 끄지 않아도 됩니다"
    return "점검이 끝났습니다. 봇이 서버 상태를 확인하고 게임을 다시 켭니다"


def show_maintenance_popup():
    popup("점검 공지", [put_scope(MAINT_POPUP_SCOPE)])
    _put_maintenance_status()


def _put_maintenance_status():
    # Snapshot first: the checker runs in other threads
    status, _, marker = maintenance_checker.snapshot()
    notice = maintenance_checker.notice
    fetched_at = maintenance_checker.fetched_at
    error = maintenance_checker.error
    unparsed = maintenance_checker.latest_unparsed
    finished_at = maintenance_checker.finished_at
    instances = list(maintenance_checker.instances)

    with use_scope(MAINT_POPUP_SCOPE, clear=True):
        put_text(maintenance_headline(status, marker))
        if notice:
            put_text(f"실시 시간: {window_text(notice)} (일본 시간 = 한국 시간)")
            flag = " [연장 공지]" if notice["extended"] else ""
            put_text(f"공지: {notice['title']}{flag}")
            if finished_at is not None:
                put_text(f"종료 시각 {finished_at.astimezone().strftime('%H:%M')} (서버 상태 API)")
        put_text(f"마지막 공지 확인: {_local(fetched_at)}")
        if error:
            put_text(f"공지 가져오기 실패: {error}")
        if instances and not any(maintenance_checker._has_provider(i.get("push")) for i in instances):
            put_text("폰 알림 꺼짐: 설정 > 오류 알림 설정(OnePush)에서 켤 수 있습니다")
        url = None
        if notice:
            url = notice["url"]
        elif unparsed:
            url = unparsed["url"]
        if url:
            put_link("공지 열기", url=url, new_window=True)
        put_button("지금 다시 확인", onclick=_maint_recheck, small=True)


def _maint_recheck():
    # Synchronous like _recheck(): one request, 15 s worst case, throttled.
    with use_scope(MAINT_POPUP_SCOPE, clear=True):
        put_text("확인 중...")
    if not maintenance_checker.recheck():
        toast("방금 확인했습니다 — 잠시 후 다시 시도하세요", duration=3, position="right", color="warn")
    _put_maintenance_status()


def _end_hm(end_iso):
    try:
        return datetime.fromisoformat(end_iso).astimezone().strftime("%H:%M")
    except Exception:
        return "?"


def maintenance_toast(gui, state):
    """
    Per-session toast on maintenance start, extension and end. `gui` is the
    AlasGUI instance; the last seen state lives on it so every browser
    session decides on its own. The first poll only records.
    """
    if state == -1:
        return
    try:
        prev = getattr(gui, "_mt_alerted", None)
        gui._mt_alerted = state
        plan = toast_plan(prev, state)
        if plan is None:
            return
        kind, end_iso = plan
        end = _end_hm(end_iso)
        if kind == "start":
            toast(
                f"점검 시작, {end}까지 봇이 기다립니다",
                duration=30,
                position="right",
                color="error",
                onclick=show_maintenance_popup,
            )
        elif kind == "extended":
            toast(
                f"점검 연장, {end}까지 봇이 기다립니다",
                duration=30,
                position="right",
                color="warn",
                onclick=show_maintenance_popup,
            )
        else:
            toast(
                "점검 종료, 봇이 곧 게임을 다시 켭니다",
                duration=10,
                position="right",
                color="success",
                onclick=show_maintenance_popup,
            )
    except Exception as e:
        logger.warning(f"maintenance toast failed, {e!r}")
