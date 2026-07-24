"""
微信 ↔ AstrBot 桥接（OneBot v11 版）
=====================================
消息接收：WeFlow SSE 推送
AI 服务：AstrBot 通过 aiocqhttp (OneBot v11) 接入
消息发送：bridge 接收 AstrBot 的 API 调用 → WeFlow API / UIA

架构：
  WeFlow ──SSE──→ bridge.py ──WS 客户端──→ AstrBot (aiocqhttp 服务端)
                   ↑ 连接 ws://127.0.0.1:19777  ↑ 监听端口，等待客户端连入
                   发送 OneBot 事件             返回 API 响应
"""

# 共享状态：所有模块通过 import state 访问这些变量
import hashlib
import json
import os
import threading
from typing import Optional

# ============ 状态控制 ============

running = False
paused = threading.Event()
paused.clear()
run_lock = threading.Lock()
bridge_thread = None

# ============ OneBot WebSocket 客户端管理 ============

_ob_ws = None          # WebSocket 连接实例
_ob_ws_loop = None     # 事件循环
_ob_ws_ready = threading.Event()
_self_id_int = 0       # 启动时从 config 初始化


def _wxid_to_int(wxid: str) -> int:
    """将微信 wxid 映射为稳定的整数 ID。"""
    digest = hashlib.sha256(str(wxid).encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF) or 1


# ============ 桥接实例 / 发送器 ============

bridge_instance = None
bridge_lock = threading.Lock()
sender_instance = None
_ob_id_to_contact: dict[int, str] = {}  # OneBot user_id/group_id → 微信联系名
_ob_id_metadata: dict[int, dict] = {}
_route_lock = threading.RLock()
_route_file = ""
ob_client_started = False

# 群聊回复模式（运行时可变，启动时从 config 初始化）
group_reply_mode = "mention"


def init_route_store(path: str) -> None:
    """加载持久化的 OneBot ID → 微信会话路由。"""
    global _route_file
    _route_file = os.path.abspath(path)
    os.makedirs(os.path.dirname(_route_file), exist_ok=True)
    if not os.path.exists(_route_file):
        return

    try:
        with open(_route_file, "r", encoding="utf-8") as f:
            routes = json.load(f).get("routes", {})
        with _route_lock:
            for raw_id, metadata in routes.items():
                ob_id = int(raw_id)
                if not isinstance(metadata, dict):
                    continue
                contact = str(metadata.get("contact", "")).strip()
                if contact:
                    _ob_id_to_contact[ob_id] = contact
                    _ob_id_metadata[ob_id] = metadata
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # 路由缓存损坏不应阻止桥接启动；后续会由 WeFlow 会话列表重建。
        return


def _save_routes_locked() -> None:
    if not _route_file:
        return
    payload = {
        "version": 1,
        "routes": {str(key): value for key, value in _ob_id_metadata.items()},
    }
    temp_path = f"{_route_file}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, _route_file)


def register_contact(
    ob_id: int,
    contact: str,
    *,
    session_id: str = "",
    kind: str = "",
    display_name: str = "",
    ambiguous: bool = False,
    sendable: bool = True,
) -> None:
    """注册并持久化 AstrBot 回复所需的微信显示名。"""
    ob_id = int(ob_id)
    contact = str(contact).strip()
    if not contact:
        return
    metadata = {
        "contact": contact,
        "session_id": str(session_id),
        "kind": str(kind),
        "display_name": str(display_name or contact),
        "ambiguous": bool(ambiguous),
        "sendable": bool(sendable),
    }
    with _route_lock:
        changed = _ob_id_metadata.get(ob_id) != metadata
        _ob_id_to_contact[ob_id] = contact
        _ob_id_metadata[ob_id] = metadata
        if changed:
            try:
                _save_routes_locked()
            except OSError:
                pass


def resolve_contact(ob_id: int) -> Optional[str]:
    """返回可安全搜索的微信联系名；重名会话拒绝自动发送。"""
    try:
        ob_id = int(ob_id)
    except (TypeError, ValueError):
        return None
    with _route_lock:
        metadata = _ob_id_metadata.get(ob_id, {})
        if metadata.get("ambiguous") or metadata.get("sendable") is False:
            return None
        return _ob_id_to_contact.get(ob_id)


def get_route_metadata(ob_id: int) -> dict:
    try:
        ob_id = int(ob_id)
    except (TypeError, ValueError):
        return {}
    with _route_lock:
        return dict(_ob_id_metadata.get(ob_id, {}))


def list_route_metadata(kind: str = "") -> list[tuple[int, dict]]:
    with _route_lock:
        return [
            (ob_id, dict(metadata))
            for ob_id, metadata in _ob_id_metadata.items()
            if not kind or metadata.get("kind") == kind
        ]
