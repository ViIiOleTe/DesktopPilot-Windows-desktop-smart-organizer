"""Explicitly simulated desktop for automated tests and --smoke-test only.

The normal application never selects this backend automatically.
"""
from dataclasses import replace

from .models import DesktopState, Icon, Monitor, Point, Rect


def sample_desktop() -> DesktopState:
    """Return fictional Shell metadata; these paths are never opened."""
    return DesktopState(
        icons=(
            Icon("file:fixture:1", "项目资料", r"C:\Users\Demo\Desktop\项目资料", Point(384, 208), is_folder=True),
            Icon("file:fixture:2", "编辑器", r"C:\Users\Demo\Desktop\编辑器.lnk", Point(96, 416), is_shortcut=True, target=r"C:\Apps\Editor\editor.exe"),
            Icon("file:fixture:3", "示例游戏", r"C:\Users\Demo\Desktop\示例游戏.url", Point(576, 312), is_shortcut=True, target="steam://rungameid/12345"),
            Icon("file:fixture:4", "计划.pdf", r"C:\Users\Demo\Desktop\计划.pdf", Point(288, 520)),
            Icon("shell:fixture:recycle", "回收站", "::{645FF040-5081-101B-9F08-00AA002F954E}", Point(768, 104), is_folder=True, is_filesystem=False),
            Icon("file:fixture:public", "公共浏览器", r"C:\Users\Public\Desktop\公共浏览器.lnk", Point(1440, 208), is_shortcut=True, target=r"C:\Apps\Browser\browser.exe"),
            Icon("shell:fixture:computer", "此电脑", "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}", Point(1632, 416), is_folder=True, is_filesystem=False),
        ),
        monitors=(Monitor("DEMO-1", Rect(0, 0, 1280, 720), 96), Monitor("DEMO-2", Rect(1280, 0, 2304, 768), 144)),
        spacing=Point(96, 104), icon_size=48,
    )


class SimulatedBackend:
    def __init__(self, state: DesktopState | None = None):
        self.state = state or sample_desktop()
        self.position_calls: list[dict[str, Point]] = []

    def read(self) -> DesktopState:
        return self.state

    def position(self, positions: dict[str, Point]) -> DesktopState:
        self.position_calls.append(dict(positions))
        self.state = replace(
            self.state,
            icons=tuple(replace(icon, position=positions.get(icon.identity, icon.position)) for icon in self.state.icons),
        )
        return self.state
