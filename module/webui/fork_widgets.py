from pywebio.output import use_scope

from module.webui.utils import Switch
from module.webui.widgets import put_icon_buttons

# Outlined square, drawn in the same wrapper as assets/gui/icon/*.svg so the
# existing `.aside-icon` sizing and per-theme `path` fill apply unchanged.
ICON_STOP = (
    '<svg class="aside-icon icon-stop" viewBox="0 0 1024 1024" version="1.1"'
    ' xmlns="http://www.w3.org/2000/svg"><path d="M810.666667 128a85.333333'
    ' 85.333333 0 0 1 85.333333 85.333333v597.333334a85.333333 85.333333 0 0'
    ' 1-85.333333 85.333333H213.333333a85.333333 85.333333 0 0 1-85.333333-85.333333V213.333333a85.333333'
    ' 85.333333 0 0 1 85.333333-85.333333h597.333334z m0 64H213.333333a21.333333'
    ' 21.333333 0 0 0-21.333333 21.333333v597.333334a21.333333 21.333333 0 0 0'
    ' 21.333333 21.333333h597.333334a21.333333 21.333333 0 0 0 21.333333-21.333333V213.333333a21.333333'
    ' 21.333333 0 0 0-21.333333-21.333333z"></path></svg>'
)


class IconSwitchButton(Switch):
    """
    Same two-state behaviour as widgets.BinarySwitchButton, but rendered like
    the other aside entries (icon above, small label below) via
    put_icon_buttons(), which has no scope argument and must therefore be
    wrapped in use_scope().
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
            0: {"func": self.update_button, "args": (label_off, icon_off, onclick_off)},
            1: {"func": self.update_button, "args": (label_on, icon_on, onclick_on)},
        }
        super().__init__(status=status, get_state=get_state, name=scope)

    def update_button(self, label, icon, onclick):
        with use_scope(self.scope, clear=True):
            put_icon_buttons(
                icon,
                buttons=[{"label": label, "value": self.value, "color": "aside"}],
                onclick=[onclick],
            )
