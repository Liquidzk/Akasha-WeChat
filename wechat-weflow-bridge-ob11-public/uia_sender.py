"""
uia_sender.py — 基于 Windows UI Automation 的微信 4.0+ 消息发送器
=================================================================

原理：
  微信 4.0 基于 Electron (Chromium)。Chromium 通过 UIA 桥将 HTML 输入元素
  暴露为标准 UIA 控件。通过 ValuePattern 设置输入框文本，InvokePattern 点击
  发送按钮。全程无鼠标键盘模拟，无 DLL 注入，风控风险极低。

工作流：
  1. 定位微信 4.0 窗口 (Electron/Chromium)
  2. 搜索联系人 → 点击匹配项 → 切换到目标聊天
  3. 定位聊天输入框 (EditControl + ValuePattern)
  4. 设置文本 → 点击发送按钮或 Enter
  5. 图片通过剪贴板粘贴后发送

依赖:
  pip install uiautomation pyperclip
  发送图片需要 Pillow: pip install Pillow
"""

import logging
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("weflow-bridge")


class BaseSender:
    """消息发送器基类"""
    def send_text(
        self,
        contact: str,
        text: str,
        is_group: bool | None = None,
    ) -> bool:
        raise NotImplementedError

    def send_image(
        self,
        contact: str,
        image_path: str,
        is_group: bool | None = None,
    ) -> bool:
        raise NotImplementedError


class UiaSender(BaseSender):
    """
    基于 Windows UI Automation 的微信 4.0+ 发送器

    对微信 4.0 (Electron/Chromium) 优化：
      - 自动检测 Electron 架构
      - ValuePattern 直接设值（非键盘模拟）
      - InvokePattern 精确点击发送按钮
      - 自动联系人搜索切换

    Attributes:
        search_enabled: 是否自动搜索联系人（默认 True，False 则需手动切到聊天窗口）
    """

    WECHAT_TITLES = ["微信", "WeChat"]

    EXCLUDE_CLASSES = ["Chrome_WidgetWin_1", "CabinetWClass"]

    def __init__(self, search_enabled: bool = True):
        self._lock = threading.Lock()
        self._auto = None
        self._ready = False

        # 微信窗口
        self._window = None
        self._is_electron = False  # True=4.0+, False=3.9

        # 控件缓存
        self._search_box = None
        self._input_control = None
        self._send_button = None
        self._last_contact = ""
        self._use_coord_fallback = False

        self.search_enabled = search_enabled

        self._init()

    # ================================================================
    # 初始化
    # ================================================================

    def _init(self):
        """初始化 UIA 并定位窗口"""
        try:
            import uiautomation as auto
            self._auto = auto
        except ImportError:
            log.error("请先安装 uiautomation: pip install uiautomation")
            return

        log.info("正在搜索微信窗口...")
        self._find_window()
        if self._window:
            log.info(f"微信窗口: '{self._window.Name}' ClassName={self._window.ClassName}")
            self._ready = True

    @staticmethod
    def _mmui_tree_available(window) -> bool:
        found = []

        def walk(control, depth=0):
            if depth > 20 or found:
                return
            try:
                if (
                    (control.AutomationId or "") == "session_list"
                    or control.ClassName == "mmui::ChatMasterView"
                ):
                    found.append(True)
                    return
                for child in control.GetChildren():
                    walk(child, depth + 1)
            except Exception:
                pass

        walk(window)
        return bool(found)

    def _restore_mmui_from_tray(self):
        """从系统托盘恢复微信 4 主窗口。"""
        import ctypes
        from ctypes import wintypes

        auto = self._auto
        overflow_classes = {
            "TopLevelWindowForOverflowXamlIsland",
            "NotifyIconOverflowWindow",
        }

        def root_children():
            return auto.GetRootControl().GetChildren()

        def find_descendant(control, predicate, max_depth=12):
            matches = []

            def walk(item, depth=0):
                if depth > max_depth or matches:
                    return
                try:
                    if predicate(item):
                        matches.append(item)
                        return
                    for child in item.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    pass

            walk(control)
            return matches[0] if matches else None

        def find_window():
            for item in root_children():
                if (
                    item.ClassName == "mmui::MainWindow"
                    and self._mmui_tree_available(item)
                ):
                    return item
            return None

        def wait_for_window(timeout=3.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                window = find_window()
                if window:
                    return window
                time.sleep(0.2)
            return None

        def post_click(control):
            hwnd = int(control.GetTopLevelControl().NativeWindowHandle or 0)
            if not hwnd:
                return
            origin = wintypes.POINT(0, 0)
            if not ctypes.windll.user32.ClientToScreen(
                hwnd,
                ctypes.byref(origin),
            ):
                return
            rect = control.BoundingRectangle
            x = int(rect.left + rect.width() / 2 - origin.x)
            y = int(rect.top + rect.height() / 2 - origin.y)
            lparam = (y << 16) | (x & 0xFFFF)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0200, 0, lparam)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0201, 1, lparam)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0202, 0, lparam)

        children = root_children()
        overflow_windows = [
            item for item in children if item.ClassName in overflow_classes
        ]
        taskbars = [
            item for item in children if item.ClassName == "Shell_TrayWnd"
        ]
        if not overflow_windows and taskbars:
            hidden_button = find_descendant(
                taskbars[0],
                lambda item: (
                    item.ControlTypeName == "ButtonControl"
                    and (item.Name or "").strip()
                    in {"显示隐藏的图标", "Show hidden icons"}
                ),
            )
            if hidden_button:
                invoke = hidden_button.GetInvokePattern()
                if invoke:
                    invoke.Invoke()
                else:
                    post_click(hidden_button)
                time.sleep(1.0)
                overflow_windows = [
                    item
                    for item in root_children()
                    if item.ClassName in overflow_classes
                ]

        search_roots = [*overflow_windows, *taskbars]
        weixin_icon = next(
            (
                icon
                for root in search_roots
                if (
                    icon := find_descendant(
                        root,
                        lambda item: (
                            item.ControlTypeName == "ButtonControl"
                            and (item.Name or "").strip()
                            in {"微信", "WeChat"}
                        ),
                    )
                )
            ),
            None,
        )
        if not weixin_icon:
            return None

        invoke = weixin_icon.GetInvokePattern()
        if invoke:
            invoke.Invoke()
            window = wait_for_window()
            if window:
                log.info("已从系统托盘恢复微信窗口")
                return window

        legacy = weixin_icon.GetLegacyIAccessiblePattern()
        if legacy:
            legacy.DoDefaultAction()
            window = wait_for_window()
            if window:
                log.info("已从系统托盘恢复微信窗口")
                return window

        post_click(weixin_icon)
        window = wait_for_window()
        if window:
            log.info("已从系统托盘恢复微信窗口")
        return window

    def _find_window(self):
        """按标题搜索微信窗口"""
        auto = self._auto
        root = auto.GetRootControl()
        candidates = []
        for w in root.GetChildren():
            cls = w.ClassName
            if cls in self.EXCLUDE_CLASSES:
                continue
            for kw in self.WECHAT_TITLES:
                if kw in w.Name:
                    if cls in {
                        "mmui::MainWindow",
                        "Qt51514QWindowIcon",
                        "WeChatMainWndForPC",
                    }:
                        candidates.append(w)
                    break
        candidates = [
            item
            for item in candidates
            if (
                item.ClassName != "mmui::MainWindow"
                or self._mmui_tree_available(item)
            )
        ]
        if not candidates:
            try:
                import ctypes

                hwnd = ctypes.windll.user32.FindWindowW(
                    "mmui::MainWindow",
                    None,
                )
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)
                    time.sleep(0.8)
                    candidate = auto.ControlFromHandle(hwnd)
                    if self._mmui_tree_available(candidate):
                        candidates.append(candidate)
            except Exception:
                pass
        if not candidates:
            try:
                restored = self._restore_mmui_from_tray()
                if restored:
                    candidates.append(restored)
            except Exception as exc:
                log.debug("从系统托盘恢复微信失败: %s", exc)
        if not candidates:
            try:
                import ctypes

                key_up = 0x0002
                for virtual_key in (0x11, 0x12, 0x57):
                    ctypes.windll.user32.keybd_event(
                        virtual_key,
                        0,
                        0,
                        0,
                    )
                for virtual_key in (0x57, 0x12, 0x11):
                    ctypes.windll.user32.keybd_event(
                        virtual_key,
                        0,
                        key_up,
                        0,
                    )
                time.sleep(1.5)
                candidates = [
                    item
                    for item in auto.GetRootControl().GetChildren()
                    if item.ClassName == "mmui::MainWindow"
                ]
            except Exception:
                pass
        if not candidates:
            self._window = None
            return
        self._window = max(
            candidates,
            key=lambda item: (
                item.ClassName == "mmui::MainWindow",
                item.BoundingRectangle.width() * item.BoundingRectangle.height(),
            ),
        )
        self._is_electron = self._window.ClassName != "WeChatMainWndForPC"

    # ================================================================
    # 控件定位
    # ================================================================

    def _ensure_window(self) -> bool:
        """确保窗口可用"""
        if self._window and self._window.Exists(0.2):
            if (
                self._window.ClassName != "mmui::MainWindow"
                or self._mmui_tree_available(self._window)
            ):
                self._ready = True
                return True
        self._window = None
        self._find_window()
        if not self._window:
            log.warning("微信窗口未找到")
            self._ready = False
            return False
        log.info(
            "已重新绑定微信窗口: '%s' ClassName=%s",
            self._window.Name,
            self._window.ClassName,
        )
        self._last_contact = ""
        self._reset_input_cache()
        self._ready = True
        return True

    def _get_hwnd(self) -> int:
        """获取当前微信主窗口句柄，兼容微信 3.9、4.0 和 4.1。"""
        try:
            hwnd = int(self._window.NativeWindowHandle or 0)
            if hwnd:
                return hwnd
        except Exception:
            pass
        try:
            import ctypes
            for class_name in (
                "mmui::MainWindow",
                "Qt51514QWindowIcon",
                "WeChatMainWndForPC",
            ):
                hwnd = ctypes.windll.user32.FindWindowW(class_name, None)
                if hwnd:
                    return int(hwnd)
        except Exception:
            pass
        return 0

    def _activate(self):
        """激活微信窗口到前台（AttachThreadInput 确保后台也能生效）"""
        try:
            self._window.SetActive()
            time.sleep(0.3)
        except Exception:
            try:
                self._window.SwitchToThisWindow()
                time.sleep(0.3)
            except Exception:
                pass
        # AttachThreadInput 绕过 Windows 后台进程不能 SetForegroundWindow 的限制
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = self._get_hwnd()
            if hwnd:
                WE_CHAT_TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
                CURRENT_TID = ctypes.windll.kernel32.GetCurrentThreadId()
                ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, True)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.BringWindowToTop(hwnd)
                ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, False)
        except Exception:
            pass

    def _dump_tree(self, ctrl, depth: int = 0, max_depth: int = 4):
        """调试: 输出 UIA 子树（仅 debug）"""
        if depth > max_depth:
            return
        try:
            pad = "  " * depth
            name = (ctrl.Name or "")[:40]
            cls = ctrl.ClassName or ""
            ctrl_type = ctrl.ControlTypeName
            vp = ctrl.IsValuePatternAvailable if hasattr(ctrl, 'IsValuePatternAvailable') else '?'
            ip = ctrl.IsInvokePatternAvailable if hasattr(ctrl, 'IsInvokePatternAvailable') else '?'
            rect = ctrl.BoundingRectangle
            info = f"[{rect.left},{rect.top} {rect.width()}x{rect.height()}]" if rect else ""
            log.debug(f"{pad}{ctrl_type} '{name}' {info} V={vp} I={ip} cls={cls}")
            for child in ctrl.GetChildren():
                self._dump_tree(child, depth + 1, max_depth)
        except Exception:
            pass

    def _find_search_box_uia(self):
        """
        通过 UIA 树定位微信搜索框。

        微信 4.x (Qt/Electron) 的搜索框特征：
        - EditControl 类型
        - 窗口上半部分 (top < 30% 窗口高度)
        - 宽度小于窗口一半（区别于底部的聊天输入框）
        - 宽度大于 50px（排除小控件）
        """
        auto = self._auto
        win_rect = self._window.BoundingRectangle
        win_w = win_rect.width()
        win_h = win_rect.height()

        edits = []

        def walk(ctrl, depth=0):
            if depth > 12:
                return
            try:
                for child in ctrl.GetChildren():
                    if child.ControlTypeName == "EditControl":
                        rect = child.BoundingRectangle
                        if rect and rect.width() > 50:
                            edits.append((child, rect))
                    walk(child, depth + 1)
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception:
            pass

        # 过滤：上半部分的 EditControl，宽度小于窗口一半
        candidates = [
            (c, r) for c, r in edits
            if r.top < win_rect.top + win_h * 0.3 and r.width() < win_w * 0.5
        ]

        if not candidates:
            return None

        # 取最靠上的（搜索框通常比任何其他上半部分控件更高）
        candidates.sort(key=lambda x: x[1].top)
        return candidates[0][0]

    def _focus_chat_input(self):
        """
        物理点击聊天输入框区域（坐标后备模式专用）。
        让聊天输入框获得键盘焦点。
        """
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return

        hwnd = self._get_hwnd()
        if not hwnd:
            return

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        input_x = rect.left + int(win_w * 0.3)
        input_y = rect.top + int(win_h * 0.92)
        ctypes.windll.user32.SetCursorPos(input_x, input_y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.3)

    def _switch_contact(
        self,
        contact: str,
        is_group: bool | None = None,
    ) -> bool:
        """
        切换到指定联系人/群聊的聊天窗口。

        Ctrl+F 搜索 → 粘贴 → Enter
        """
        if not self._ensure_window():
            return False
        self._activate()

        if self._window.ClassName == "mmui::MainWindow":
            return self._switch_contact_mmui(contact, is_group)

        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return False

        hwnd = self._get_hwnd()
        if not hwnd:
            log.warning("找不到微信主窗口句柄")
            return False

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top

        WE_CHAT_TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        CURRENT_TID = ctypes.windll.kernel32.GetCurrentThreadId()
        ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, True)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        time.sleep(0.3)

        try:
            # Ctrl+F 打开搜索
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
            ctypes.windll.user32.keybd_event(0x46, 0, 0, 0)   # F
            ctypes.windll.user32.keybd_event(0x46, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.5)

            # 清空搜索框
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
            ctypes.windll.user32.keybd_event(0x41, 0, 0, 0)   # A
            ctypes.windll.user32.keybd_event(0x41, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.15)

            # 粘贴联系人/群名
            import pyperclip
            pyperclip.copy(contact)
            time.sleep(0.1)
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
            ctypes.windll.user32.keybd_event(0x56, 0, 0, 0)   # V
            ctypes.windll.user32.keybd_event(0x56, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.3)

            # Enter → 选中第一个结果
            ctypes.windll.user32.keybd_event(0x0D, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x0D, 0, 2, 0)
            time.sleep(0.8)

            log.info(f"已切到联系人: {contact}")
            return True
        finally:
            ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, False)

    def _switch_contact_mmui(
        self,
        contact: str,
        is_group: bool | None = None,
    ) -> bool:
        """通过微信 4.1 mmui 控件搜索并精确选择会话。"""
        recent_items = self._find_mmui_list_items(
            lambda item: (item.AutomationId or "") == f"session_item_{contact}"
        )
        for recent_item in recent_items:
            for attempt in range(2):
                if not self._activate_mmui_list_item(recent_item):
                    break
                self._reset_input_cache()
                if self._wait_mmui_chat_ready(contact, is_group):
                    log.info(f"已从会话列表切到联系人: {contact}")
                    return True
                if attempt == 0:
                    log.debug("会话首次打开未就绪，正在重试: %s", contact)

        search_box = self._find_search_box_uia()
        if not search_box:
            try:
                self._auto.SendKeys("{Ctrl}f")
                time.sleep(0.8)
                search_box = self._find_search_box_uia()
            except Exception:
                pass
        if not search_box:
            log.error("微信 4.1 搜索框未找到")
            return False

        try:
            value_pattern = self._get_value_pattern(search_box)
            if value_pattern:
                value_pattern.SetValue(contact)
            else:
                import pyperclip
                search_box.Click()
                pyperclip.copy(contact)
                search_box.SendKeys("{Ctrl}a")
                search_box.SendKeys("{Ctrl}v")
            time.sleep(0.8)

            candidates = self._find_mmui_list_items(
                lambda item: (
                    (item.Name or "") == contact
                    or (item.Name or "").startswith(f"{contact} ")
                )
            )
            if not candidates:
                log.error(f"微信 4.1 未找到精确会话: {contact}")
                return False

            if is_group is None:
                try:
                    search_box.SetFocus()
                    search_box.SendKeys("{Enter}")
                    self._reset_input_cache()
                    if self._wait_mmui_chat_ready(
                        contact,
                        timeout=3.0,
                    ):
                        log.info(f"已通过搜索切到联系人: {contact}")
                        return True
                except Exception:
                    pass

            for candidate in candidates:
                if not self._activate_mmui_list_item(candidate):
                    continue
                self._reset_input_cache()
                if self._wait_mmui_chat_ready(contact, is_group):
                    log.info(f"已切到联系人: {contact}")
                    return True
            log.error(f"微信 4.1 会话未成功打开: {contact}")
            return False
        except Exception as exc:
            log.error(f"微信 4.1 切换会话失败: {contact}: {exc}")
            return False

    def _find_mmui_list_item(self, predicate):
        matches = self._find_mmui_list_items(predicate)
        return matches[0] if matches else None

    def _find_mmui_list_items(self, predicate):
        return self._find_mmui_controls(
            lambda control: (
                control.ControlTypeName == "ListItemControl"
                and predicate(control)
            ),
            max_depth=18,
        )

    def _find_mmui_control(self, predicate, max_depth: int = 24):
        matches = self._find_mmui_controls(predicate, max_depth)
        return matches[0] if matches else None

    def _find_mmui_controls(self, predicate, max_depth: int = 24):
        matches = []

        def walk(ctrl, depth=0):
            if depth > max_depth:
                return
            try:
                for child in ctrl.GetChildren():
                    if predicate(child):
                        matches.append(child)
                    walk(child, depth + 1)
            except Exception:
                pass

        walk(self._window)
        return matches

    def _current_mmui_is_group(self, contact: str) -> bool | None:
        title = self._find_mmui_control(
            lambda control: (
                control.ControlTypeName == "TextControl"
                and control.ClassName == "mmui::XHBoxView"
                and (control.AutomationId or "").endswith(
                    "big_title_line_h_view"
                )
            )
        )
        if not title:
            return None
        return bool(
            re.fullmatch(
                rf"{re.escape(contact)}\(\d+\)",
                (title.Name or "").strip(),
            )
        )

    def _activate_mmui_list_item(self, item) -> bool:
        try:
            if self._post_mmui_click(item):
                time.sleep(0.8)
                return True
        except Exception:
            pass

        selected = False
        try:
            pattern = item.GetSelectionItemPattern()
            if pattern:
                pattern.Select()
                time.sleep(0.3)
                selected = True
        except Exception:
            pass
        try:
            pattern = item.GetInvokePattern()
            if pattern:
                pattern.Invoke()
                time.sleep(0.5)
                selected = True
        except Exception:
            pass
        try:
            pattern = item.GetLegacyIAccessiblePattern()
            if pattern:
                pattern.DoDefaultAction()
                time.sleep(0.5)
                selected = True
        except Exception:
            pass
        try:
            item.SetFocus()
            item.SendKeys("{Enter}")
            time.sleep(0.5)
            selected = True
        except Exception:
            pass
        try:
            item.Click()
            selected = True
        except Exception:
            pass
        try:
            import ctypes
            self._activate()
            rect = item.BoundingRectangle
            x = int(rect.left + rect.width() / 2)
            y = int(rect.top + rect.height() / 2)
            ctypes.windll.user32.SetCursorPos(x, y)
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.8)
            return True
        except Exception:
            return selected

    def _post_mmui_click(self, control) -> bool:
        """向 Qt 主窗口投递点击，兼容已断开的 RDP 会话。"""
        import ctypes
        from ctypes import wintypes

        hwnd = self._get_hwnd()
        if not hwnd:
            return False
        control_rect = control.BoundingRectangle
        client_origin = wintypes.POINT(0, 0)
        if not ctypes.windll.user32.ClientToScreen(
            hwnd,
            ctypes.byref(client_origin),
        ):
            return False
        x = int(
            control_rect.left + control_rect.width() / 2 - client_origin.x
        )
        y = int(
            control_rect.top + control_rect.height() / 2 - client_origin.y
        )
        log.debug(
            "mmui 后台点击: hwnd=%s client=(%s,%s) screen=(%s,%s) "
            "relative=(%s,%s)",
            hwnd,
            client_origin.x,
            client_origin.y,
            int(control_rect.left + control_rect.width() / 2),
            int(control_rect.top + control_rect.height() / 2),
            x,
            y,
        )
        lparam = (y << 16) | (x & 0xFFFF)
        ctypes.windll.user32.PostMessageW(hwnd, 0x0200, 0, lparam)
        ctypes.windll.user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)
        ctypes.windll.user32.PostMessageW(hwnd, 0x0202, 0, lparam)
        return True

    def _post_mmui_key(self, virtual_key: int) -> bool:
        import ctypes

        hwnd = self._get_hwnd()
        if not hwnd:
            return False
        ctypes.windll.user32.PostMessageW(hwnd, 0x0100, virtual_key, 0)
        ctypes.windll.user32.PostMessageW(hwnd, 0x0101, virtual_key, 0)
        return True

    def _post_mmui_paste(self) -> bool:
        import ctypes

        hwnd = self._get_hwnd()
        if not hwnd:
            return False
        ctypes.windll.user32.SendMessageW(hwnd, 0x0302, 0, 0)
        return True

    def _ensure_target_contact(
        self,
        contact: str,
        is_group: bool | None = None,
    ) -> bool:
        if not self.search_enabled or not contact:
            return True
        if contact == self._last_contact:
            if self._window.ClassName != "mmui::MainWindow":
                return True
            if self._wait_mmui_chat_ready(
                contact,
                is_group,
                timeout=0.8,
            ):
                return True
        if not self._switch_contact(contact, is_group):
            return False
        self._last_contact = contact
        return True

    def _get_mmui_session_preview(self, contact: str) -> str:
        item = self._find_mmui_list_item(
            lambda control: (
                (control.AutomationId or "") == f"session_item_{contact}"
            )
        )
        return (item.Name or "") if item else ""

    def _wait_mmui_preview(
        self,
        contact: str,
        expected: str,
        previous: str,
        timeout: float = 8.0,
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            preview = self._get_mmui_session_preview(contact)
            if expected in preview and preview != previous:
                return True
            time.sleep(0.2)
        return False

    def _send_mmui_image_via_file_dialog(
        self,
        contact: str,
        image_path: str,
        previous_preview: str,
    ) -> bool:
        def find_dialog():
            matches = []

            def walk(control, depth=0):
                if depth > 8 or matches:
                    return
                try:
                    if (
                        control.ClassName == "#32770"
                        or (control.Name or "").strip()
                        in {"选择文件", "打开", "Open"}
                    ):
                        matches.append(control)
                        return
                    for child in control.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    pass

            walk(self._window)
            if not matches:
                walk(self._auto.GetRootControl())
            return matches[0] if matches else None

        dialog = find_dialog()
        file_button = self._find_mmui_control(
            lambda control: (
                control.ControlTypeName == "ButtonControl"
                and (control.Name or "").strip() == "发送文件"
            )
        )
        if not dialog and not file_button:
            log.error("微信 4.1 未找到发送文件按钮")
            return False
        if not dialog:
            if not self._post_mmui_click(file_button):
                return False
            deadline = time.time() + 6.0
            while time.time() < deadline and not dialog:
                dialog = find_dialog()
                if not dialog:
                    time.sleep(0.2)
        if not dialog:
            log.error("微信文件选择对话框未出现")
            return False

        filename_edit = None
        open_button = None

        def walk(control, depth=0):
            nonlocal filename_edit, open_button
            if depth > 14:
                return
            try:
                for child in control.GetChildren():
                    name = (child.Name or "").strip()
                    automation_id = child.AutomationId or ""
                    if child.ControlTypeName == "EditControl" and (
                        automation_id == "1148"
                        or name in {"文件名:", "File name:"}
                    ):
                        filename_edit = child
                    if child.ControlTypeName == "ButtonControl" and (
                        automation_id == "1"
                        or name in {"打开", "打开(O)", "Open"}
                    ):
                        open_button = child
                    walk(child, depth + 1)
            except Exception:
                pass

        walk(dialog)
        if not filename_edit or not open_button:
            log.error("微信文件选择对话框控件不完整")
            return False

        value_pattern = self._get_value_pattern(filename_edit)
        if not value_pattern:
            log.error("文件名输入框不支持 ValuePattern")
            return False
        value_pattern.SetValue(os.path.abspath(image_path))
        invoke = open_button.GetInvokePattern()
        if invoke:
            invoke.Invoke()
        else:
            open_button.Click()

        # WeChat 4 normally opens an attachment preview after file selection.
        # Click its Send button as soon as it appears instead of waiting for the
        # session preview timeout first.
        preview_deadline = time.time() + 3.0
        while time.time() < preview_deadline:
            preview = self._get_mmui_session_preview(contact)
            if "[图片]" in preview and preview != previous_preview:
                return True

            send_button = self._find_mmui_control(
                lambda control: (
                    control.ControlTypeName == "ButtonControl"
                    and control.ClassName == "mmui::XOutlineButton"
                    and (control.Name or "").strip() == "发送"
                )
            )
            if send_button:
                if not self._post_mmui_click(send_button):
                    return False
                return self._wait_mmui_preview(
                    contact,
                    "[图片]",
                    previous_preview,
                    timeout=10.0,
                )
            time.sleep(0.15)

        return self._wait_mmui_preview(
            contact,
            "[图片]",
            previous_preview,
            timeout=8.0,
        )

    def _reset_input_cache(self) -> None:
        self._input_control = None
        self._send_button = None
        self._use_coord_fallback = False

    def _wait_mmui_chat_ready(
        self,
        contact: str = "",
        is_group: bool | None = None,
        timeout: float = 5.0,
    ) -> bool:
        deadline = time.time() + timeout
        wrong_kind_observations = 0
        while time.time() < deadline:
            ready = []

            def walk(ctrl, depth=0):
                if depth > 24 or ready:
                    return
                try:
                    automation_id = ctrl.AutomationId or ""
                    if automation_id == "chat_input_field":
                        chat_name = (ctrl.Name or "").strip()
                        if not contact or chat_name == contact:
                            ready.append(True)
                            return
                    if ctrl.ClassName == "mmui::ChatPage":
                        children = ctrl.GetChildren()
                        if len(children) > 1 or (
                            children and children[0].ClassName != "mmui::XIcon"
                        ):
                            ready.append(True)
                            return
                    for child in ctrl.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    pass

            walk(self._window)
            if ready:
                if is_group is None:
                    return True
                current_is_group = self._current_mmui_is_group(contact)
                if current_is_group is is_group:
                    return True
                if current_is_group is not None:
                    wrong_kind_observations += 1
                    if wrong_kind_observations >= 2:
                        return False
                else:
                    wrong_kind_observations = 0
            time.sleep(0.2)
        return False

    def _locate_input(self) -> bool:
        """
        定位聊天输入框和发送按钮

        在 Electron 中，聊天输入框是 EditControl (支持 ValuePattern)，
        位于窗口下半部分。
        """
        if not self._ensure_window():
            return False

        # 如果已有缓存且窗口没变，直接返回
        if self._input_control is not None:
            try:
                if self._input_control.Exists(0.2):
                    return True
            except Exception:
                self._input_control = None
                self._send_button = None

        auto = self._auto
        win_rect = self._window.BoundingRectangle
        win_center_y = win_rect.top + win_rect.height() / 2

        edits = []

        def walk(ctrl, depth=0):
            if depth > 24:
                return
            try:
                for child in ctrl.GetChildren():
                    try:
                        cn = child.ControlTypeName
                        # 输入控件
                        if cn == "EditControl":
                            edits.append(child)
                        walk(child, depth + 1)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception as e:
            log.debug(f"UIA 遍历异常: {e}")

        if not edits and self._window.ClassName == "mmui::MainWindow":
            log.error("微信 4.1 未暴露聊天输入框，已取消发送")
            return False

        if not edits:
            log.warning("未找到输入控件，使用坐标后备方案（Qt 界面）")
            self._use_coord_fallback = True
            return True

        chat_inputs = [
            edit
            for edit in edits
            if (edit.AutomationId or "") == "chat_input_field"
            and (
                not self._last_contact
                or (edit.Name or "").strip() == self._last_contact
            )
        ]
        if chat_inputs:
            self._input_control = chat_inputs[0]

        # 过滤：聊天输入框在窗口下半部分，面积较大
        candidates = [e for e in edits
                      if e.BoundingRectangle and
                      e.BoundingRectangle.top >= win_center_y - 20 and
                      e.BoundingRectangle.width() > 100]

        if not candidates:
            candidates = [e for e in edits if e.BoundingRectangle]

        # 按面积倒序，最大的就是聊天输入框
        candidates.sort(key=lambda e: e.BoundingRectangle.width() *
                        e.BoundingRectangle.height(), reverse=True)

        for ctrl in candidates:
            if self._input_control:
                break
            rect = ctrl.BoundingRectangle
            area = rect.width() * rect.height()
            if area < 200:
                continue

            name = ctrl.Name or ""
            value_pattern = self._get_value_pattern(ctrl)
            log.debug(f"输入候选: '{name[:30]}' {rect.width()}x{rect.height()} "
                      f"V={bool(value_pattern)}")

            # 优先使用支持 ValuePattern 的
            if value_pattern:
                self._input_control = ctrl
                log.info(f"聊天输入框: {rect.width()}x{rect.height()} "
                         f"(ValuePattern)")
                break

        if not self._input_control:
            # 后备：用面积最大的
            self._input_control = candidates[0] if candidates else edits[0]
            log.warning(f"输入框无 ValuePattern，使用 SendKeys 后备方案")
            log.debug(f"后备输入控件: {self._input_control.ControlTypeName} "
                      f"'{self._input_control.Name[:30]}'")

        # 查找发送按钮
        try:
            buttons = []

            def find_buttons(ctrl, depth=0):
                if depth > 8:
                    return
                try:
                    for child in ctrl.GetChildren():
                        if child.ControlTypeName == "ButtonControl":
                            bn = child.Name or ""
                            if "发送" in bn or "Send" in bn or bn.strip() == "":
                                buttons.append(child)
                        find_buttons(child, depth + 1)
                except Exception:
                    pass

            find_buttons(self._window)
            if buttons:
                self._send_button = buttons[0]
                log.info("已定位发送按钮")
            else:
                log.info("未找到发送按钮，发送时用 Enter")
        except Exception:
            pass

        return True

    @staticmethod
    def _get_value_pattern(ctrl):
        try:
            return ctrl.GetValuePattern()
        except Exception:
            return None

    # ================================================================
    # 发送方法
    # ================================================================

    def send_text(
        self,
        contact: str,
        text: str,
        is_group: bool | None = None,
    ) -> bool:
        """
        发送文本消息

        Args:
            contact: 联系人昵称/备注
            text: 消息内容
        """
        with self._lock:
            if not self._ready:
                log.error("UIA Sender 未就绪")
                return False

            if not self._ensure_window():
                return False

            # 安全检查：过滤 PIL 引用
            if "<PIL." in text or "PIL." in text:
                log.warning(f"跳过 PIL 引用消息: {text[:60]}")
                return False

            self._activate()

            # 切换到联系人（物理点击搜索框，坐标后备模式下也有效）
            if not self._ensure_target_contact(contact, is_group):
                log.error(f"无法自动切换到 '{contact}'，已取消发送")
                return False

            # 定位输入框
            if not self._locate_input():
                return False

            try:
                previous_preview = ""
                if self._window.ClassName == "mmui::MainWindow":
                    previous_preview = self._get_mmui_session_preview(contact)

                if self._use_coord_fallback:
                    # Qt 界面：点击输入框区域→剪贴板粘贴→Enter
                    import pyperclip
                    import ctypes
                    from ctypes import wintypes
                    hwnd = self._get_hwnd()
                    if hwnd:
                        rect = wintypes.RECT()
                        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        win_w = rect.right - rect.left
                        win_h = rect.bottom - rect.top
                        # 输入框大致在窗口底部居中偏左的位置
                        input_x = rect.left + int(win_w * 0.3)
                        input_y = rect.top + int(win_h * 0.92)
                        # 物理点击让输入框获得焦点（PostMessage 对 Qt 子控件无效）
                        ctypes.windll.user32.SetCursorPos(input_x, input_y)
                        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
                        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
                    time.sleep(0.3)
                    pyperclip.copy(text)
                    time.sleep(0.05)
                    self._auto.SendKeys('{Ctrl}v')
                    time.sleep(0.3)
                    self._auto.SendKeys('{Enter}')
                    log.info(f"[UIA✓] {contact}: {text[:50]}... (无鼠标模式)")
                    return True

                ctrl = self._input_control

                # 设置文本
                value_pattern = self._get_value_pattern(ctrl)
                if value_pattern:
                    try:
                        value_pattern.SetValue("")
                        time.sleep(0.02)
                    except Exception:
                        pass
                    try:
                        value_pattern.SetValue(text)
                    except Exception as e:
                        log.warning(f"SetValue 失败: {e}，尝试剪贴板")
                        import pyperclip
                        pyperclip.copy(text)
                        time.sleep(0.05)
                        ctrl.SendKeys('{Ctrl}a')
                        ctrl.SendKeys('{Ctrl}v')
                else:
                    # 没有 ValuePattern，用剪贴板
                    import pyperclip
                    pyperclip.copy(text)
                    ctrl.SendKeys('{Ctrl}a')
                    time.sleep(0.05)
                    ctrl.SendKeys('{Ctrl}v')

                time.sleep(0.1)

                # 微信 4.1 的后台窗口消息不依赖 RDP 桌面保持可见。
                if self._window.ClassName == "mmui::MainWindow":
                    ctrl.SetFocus()
                    if not self._post_mmui_key(0x0D):
                        raise RuntimeError("无法向微信投递 Enter")
                elif self._send_button:
                    self._send_button.Click()
                else:
                    ctrl.SendKeys('{Enter}')
                if (
                    self._window.ClassName == "mmui::MainWindow"
                    and not self._wait_mmui_preview(
                        contact,
                        text[:20],
                        previous_preview,
                    )
                ):
                    log.error(f"[UIA✗] 文本发送后未在会话预览中确认: {contact}")
                    return False

                log.info(f"[UIA✓] {contact}: {text[:50]}...")
                return True

            except Exception as e:
                log.error(f"[UIA✗] {contact}: {e}")
                return False

    def send_image(
        self,
        contact: str,
        image_path: str,
        is_group: bool | None = None,
    ) -> bool:
        """
        通过剪贴板发送图片

        Args:
            contact: 联系人
            image_path: 图片文件路径
        """
        with self._lock:
            if not self._ready:
                return False
            if not os.path.isfile(image_path):
                log.error(f"图片不存在: {image_path}")
                return False

            try:
                if not self._ensure_window():
                    return False
                self._activate()

                if not self._ensure_target_contact(contact, is_group):
                    log.error(f"无法自动切换到 '{contact}'，已取消图片发送")
                    return False

                if not self._locate_input():
                    return False

                previous_preview = ""
                if self._window.ClassName == "mmui::MainWindow":
                    previous_preview = self._get_mmui_session_preview(contact)
                    if not self._send_mmui_image_via_file_dialog(
                        contact,
                        image_path,
                        previous_preview,
                    ):
                        log.error(
                            f"[UIA✗] 图片发送后未在会话预览中确认: {contact}"
                        )
                        return False
                    log.info(
                        f"[UIA✓] 图片 → {contact}: "
                        f"{os.path.basename(image_path)}"
                    )
                    return True

                # 旧版微信使用剪贴板粘贴图片。
                self._copy_image_to_clipboard(image_path)
                time.sleep(0.2)

                if self._use_coord_fallback:
                    import ctypes
                    from ctypes import wintypes
                    hwnd = self._get_hwnd()
                    if hwnd:
                        rect = wintypes.RECT()
                        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        input_x = rect.left + int((rect.right - rect.left) * 0.3)
                        input_y = rect.top + int((rect.bottom - rect.top) * 0.92)
                        ctypes.windll.user32.SetCursorPos(input_x, input_y)
                        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                    time.sleep(0.3)
                    self._auto.SendKeys('{Ctrl}v')
                    time.sleep(0.5)
                    self._auto.SendKeys('{Enter}')
                    log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)} (无鼠标模式)")
                    return True

                self._input_control.SendKeys('{Ctrl}v')
                time.sleep(0.5)

                if self._send_button:
                    self._send_button.Click()
                else:
                    self._input_control.SendKeys('{Enter}')

                log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)}")
                return True

            except Exception as e:
                log.error(f"[UIA✗] 图片 → {contact}: {e}")
                return False

    def _copy_image_to_clipboard(self, path: str):
        """复制图片到剪贴板（通过 PowerShell，避免 PIL 对象被当作文本复制）"""
        abs_path = os.path.abspath(path)
        try:
            subprocess.run([
                "powershell", "-WindowStyle", "Hidden", "-Command",
                f"Add-Type -AssemblyName System.Windows.Forms;"
                f"$img = [System.Drawing.Image]::FromFile('{abs_path}');"
                f"[System.Windows.Forms.Clipboard]::SetImage($img);"
                f"$img.Dispose()"
            ], check=True, timeout=10)
            log.debug("PowerShell 已复制图片到剪贴板")
        except Exception as e:
            log.error(f"复制图片到剪贴板失败: {e}")
            raise

    # ================================================================
    # 诊断
    # ================================================================

    def diagnose(self):
        """输出诊断信息，用于调试"""
        if not self._window:
            print("✗ 未找到微信窗口")
            return

        print(f"✓ 微信窗口: '{self._window.Name}'")
        print(f"  ClassName: {self._window.ClassName}")
        print(f"  Electron: {self._is_electron}")
        print(f"  位置: [{self._window.BoundingRectangle.left},"
              f"{self._window.BoundingRectangle.top}] "
              f"{self._window.BoundingRectangle.width()}x"
              f"{self._window.BoundingRectangle.height()}")

        print("\n--- UIA 树 ---")
        self._dump_tree(self._window, max_depth=4)

        print("\n--- 控件状态 ---")
        print(f"  输入框: {'✓' if self._input_control else '✗'}")
        print(f"  发送按钮: {'✓' if self._send_button else '✗'}")
