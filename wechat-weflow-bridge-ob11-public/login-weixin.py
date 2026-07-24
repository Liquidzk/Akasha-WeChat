from __future__ import annotations

import ctypes
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto


RESULT_PATH = Path(__file__).with_name("login-weixin-result.txt")
LOGIN_TIMEOUT = 90


def find_window(class_name: str):
    windows = [
        item
        for item in auto.GetRootControl().GetChildren()
        if item.ClassName == class_name and (item.Name or "") == "微信"
    ]
    if not windows:
        return None
    return max(
        windows,
        key=lambda item: item.BoundingRectangle.width()
        * item.BoundingRectangle.height(),
    )


def find_login_button(window):
    matches = []

    def walk(control, depth: int = 0) -> None:
        if depth > 16 or matches:
            return
        for child in control.GetChildren():
            if (
                child.ControlTypeName == "ButtonControl"
                and (child.Name or "") == "登录"
            ):
                matches.append(child)
                return
            walk(child, depth + 1)

    walk(window)
    return matches[0] if matches else None


def background_click(window, control) -> None:
    hwnd = int(window.NativeWindowHandle or 0)
    if not hwnd:
        raise RuntimeError("Weixin login window has no HWND")

    control_rect = control.BoundingRectangle
    client_origin = wintypes.POINT(0, 0)
    if not ctypes.windll.user32.ClientToScreen(
        hwnd,
        ctypes.byref(client_origin),
    ):
        raise RuntimeError("Unable to resolve Weixin client origin")
    x = int(
        control_rect.left + control_rect.width() / 2 - client_origin.x
    )
    y = int(
        control_rect.top + control_rect.height() / 2 - client_origin.y
    )
    lparam = (y << 16) | (x & 0xFFFF)
    ctypes.windll.user32.ShowWindow(hwnd, 9)

    invoke = control.GetInvokePattern()
    if invoke:
        invoke.Invoke()
    legacy = control.GetLegacyIAccessiblePattern()
    if legacy:
        legacy.DoDefaultAction()
    ctypes.windll.user32.PostMessageW(hwnd, 0x0200, 0, lparam)
    ctypes.windll.user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)
    ctypes.windll.user32.PostMessageW(hwnd, 0x0202, 0, lparam)

    RESULT_PATH.write_text(
        f"clicked relative={x},{y} hwnd={hwnd}",
        encoding="utf-8",
    )


def main() -> None:
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if find_window("mmui::MainWindow"):
            RESULT_PATH.write_text("already logged in", encoding="utf-8")
            return

        login_window = find_window("mmui::LoginWindow")
        if login_window:
            login_button = find_login_button(login_window)
            if login_button:
                background_click(login_window, login_button)
                return
        time.sleep(1)

    raise RuntimeError("Weixin login window did not become ready")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        RESULT_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        raise
