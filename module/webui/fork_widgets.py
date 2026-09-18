from pywebio.output import use_scope

from module.webui.utils import Switch
from module.webui.widgets import put_icon_buttons

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
