from pywebio.output import popup, put_button, put_link, put_scope, put_text, toast, use_scope

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
        status = {
            0: {"func": self.update_button, "args": (label_off, icon_off, onclick_off, "off")},
            1: {"func": self.update_button, "args": (label_on, icon_on, onclick_on, "on")},
        }
        super().__init__(status=status, get_state=get_state, name=scope)

    def update_button(self, label, icon, onclick, marker):
        with use_scope(self.scope, clear=True):
            put_icon_buttons(
                icon,
                buttons=[{"label": label, "value": self.value, "color": "aside"}],
                onclick=[onclick],
            ).style(f"--fork-switch-{marker}--")


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
