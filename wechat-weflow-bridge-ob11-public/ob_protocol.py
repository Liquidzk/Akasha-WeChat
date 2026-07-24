"""
OneBot v11 协议处理模块。

包括：
- make_message_event() — 构造 OneBot 消息事件 JSON
- push_event() — 通过 WebSocket 推送事件给 AstrBot
- _handle_ob_api() — 处理 AstrBot 发来的 API 请求（send_msg 等）
- _extract_text() — 从 OneBot message 段提取纯文本
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import logging
from urllib.parse import unquote, urlparse

import requests

import state
import config

log = logging.getLogger("ob11-bridge")


async def _handle_ob_api(data: dict):
    """处理 AstrBot 发来的 API 请求。"""
    action = data.get("action", "")
    params = data.get("params", {})
    echo = data.get("echo", "")
    log.info(f"[OB11] API: {action} echo={echo}")

    try:
        if action in ("send_msg", "send_private_msg", "send_group_msg"):
            response = await _handle_send_api(action, params)
        elif action == "get_login_info":
            response = _ok({
                "user_id": state._self_id_int,
                "nickname": config.BOT_NICKNAMES[0] if config.BOT_NICKNAMES else "微信机器人",
            })
        elif action == "get_status":
            response = _ok({"online": bool(state.running), "good": bool(state._ob_ws_ready.is_set())})
        elif action == "get_version_info":
            response = _ok({
                "app_name": "Akasha-WeChat",
                "app_version": "2026.07-weflow4",
                "protocol_version": "v11",
            })
        elif action in ("can_send_image", "can_send_record"):
            response = _ok({"yes": action == "can_send_image"})
        elif action == "get_group_info":
            group_id = _as_int(params.get("group_id"))
            metadata = state.get_route_metadata(group_id)
            response = _ok({
                "group_id": group_id,
                "group_name": metadata.get("display_name", str(group_id)),
                "member_count": 0,
                "max_member_count": 0,
            }) if metadata else _failed("微信群路由不存在", 1404)
        elif action == "get_group_list":
            groups = [
                {
                    "group_id": group_id,
                    "group_name": metadata.get("display_name", str(group_id)),
                    "member_count": 0,
                    "max_member_count": 0,
                }
                for group_id, metadata in state.list_route_metadata("group")
            ]
            response = _ok(groups)
        elif action in ("get_group_member_info", "get_stranger_info"):
            user_id = _as_int(params.get("user_id"))
            metadata = state.get_route_metadata(user_id)
            nickname = metadata.get("display_name", str(user_id)) if metadata else str(user_id)
            member = {"user_id": user_id, "nickname": nickname, "sex": "unknown", "age": 0}
            if action == "get_group_member_info":
                member.update({
                    "group_id": _as_int(params.get("group_id")),
                    "card": nickname,
                    "role": "member",
                    "title": "",
                })
            response = _ok(member)
        else:
            response = _failed(f"不支持的 OneBot API: {action}", 1404)
    except Exception as exc:
        log.exception(f"[OB11] API 处理异常: {action}")
        response = _failed(f"OneBot API 处理异常: {exc}", 1500)

    if echo != "":
        response["echo"] = echo
    try:
        if state._ob_ws:
            await state._ob_ws.send(json.dumps(response, ensure_ascii=False))
            log.info(f"[OB11] 已回响应: {action} status={response['status']}")
        else:
            log.warning(f"[OB11] 无法回响应（WS 未连接）: {action}")
    except Exception as exc:
        log.warning(f"[OB11] 回响应失败: {action}: {exc}")


async def _handle_send_api(action: str, params: dict) -> dict:
    if action == "send_group_msg":
        is_group = True
    elif action == "send_private_msg":
        is_group = False
    else:
        message_type = str(params.get("message_type", "")).lower()
        is_group = message_type == "group" or (
            "group_id" in params and "user_id" not in params
        )

    target_key = "group_id" if is_group else "user_id"
    target_id = _as_int(params.get(target_key))
    if not target_id:
        return _failed(f"缺少 {target_key}", 1400)

    contact = state.resolve_contact(target_id)
    if not contact:
        metadata = state.get_route_metadata(target_id)
        if metadata.get("ambiguous"):
            return _failed("微信会话名称重名，已阻止自动发送", 1409)
        if metadata.get("sendable") is False:
            return _failed("WeFlow 未提供可搜索会话名，请配置 contact_overrides", 1409)
        return _failed(f"未找到微信会话路由: {target_id}", 1404)

    message = params.get("message", [])
    if isinstance(message, str):
        message = [{"type": "text", "data": {"text": message}}]
    if not isinstance(message, list):
        return _failed("message 格式无效", 1400)

    sent_any = False
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        seg_data = seg.get("data", {})

        if seg_type == "text":
            text = str(seg_data.get("text", ""))
            if text:
                _record_sent_message(text)
                sent = await asyncio.to_thread(
                    state.sender_instance.send_text,
                    contact,
                    text,
                    is_group,
                )
                if not sent:
                    return _failed(f"微信文字发送失败: {contact}", 1500)
                sent_any = True
                log.info(f"[OB11] 文字已发送至 {contact}: {text[:50]}")
        elif seg_type == "image":
            file_val = str(seg_data.get("file", ""))
            img_path, cleanup = await asyncio.to_thread(_resolve_image_file, file_val)
            if not img_path:
                return _failed("图片文件不存在或无法读取", 1404)
            try:
                _record_sent_message("[图片]")
                sent = await asyncio.to_thread(
                    state.sender_instance.send_image,
                    contact,
                    img_path,
                    is_group,
                )
                if not sent:
                    return _failed(f"微信图片发送失败: {contact}", 1500)
                sent_any = True
                log.info(f"[OB11] 图片已发送至 {contact}")
            finally:
                if cleanup:
                    try:
                        os.unlink(img_path)
                    except OSError:
                        pass
        elif seg_type == "face":
            _record_sent_message("[表情]")
            sent = await asyncio.to_thread(
                state.sender_instance.send_text,
                contact,
                "[表情]",
                is_group,
            )
            if not sent:
                return _failed(f"微信表情占位发送失败: {contact}", 1500)
            sent_any = True

    if not sent_any:
        return _failed("消息中没有可发送的文字或图片", 1400)
    return _ok({"message_id": int(time.time() * 1000)})


def _ok(payload=None) -> dict:
    return {"status": "ok", "retcode": 0, "data": {} if payload is None else payload}


def _failed(message: str, retcode: int) -> dict:
    return {
        "status": "failed",
        "retcode": retcode,
        "data": None,
        "message": message,
        "wording": message,
    }


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _record_sent_message(content: str) -> None:
    bridge = state.bridge_instance
    if bridge and hasattr(bridge, "record_sent_message"):
        bridge.record_sent_message(content)


def _resolve_image_file(file_val: str) -> tuple[str | None, bool]:
    """解析 OneBot 图片来源，返回 (路径, 是否由本函数创建)。"""
    if not file_val:
        return None, False
    if file_val.startswith("base64://"):
        try:
            return _decode_base64_image(file_val[9:]), True
        except (ValueError, OSError):
            return None, False
    if file_val.startswith(("http://", "https://")):
        try:
            response = requests.get(file_val, timeout=30)
            response.raise_for_status()
            suffix = os.path.splitext(urlparse(file_val).path)[1] or ".png"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(response.content)
            tmp.close()
            return tmp.name, True
        except requests.RequestException:
            return None, False
    if file_val.startswith("file://"):
        parsed = urlparse(file_val)
        local_path = unquote(parsed.path)
        if os.name == "nt" and local_path.startswith("/") and len(local_path) > 2:
            local_path = local_path[1:]
        file_val = local_path

    if os.path.isabs(file_val) and os.path.isfile(file_val):
        return file_val, False
    candidates = []
    if config.ASTRBOT_ATTACHMENTS:
        candidates.extend([
            os.path.join(config.ASTRBOT_ATTACHMENTS, file_val),
            os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images", file_val),
        ])
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate, False
    return None, False


def _extract_text(message: list) -> str:
    """从 OneBot message 段中提取可发送的文本。"""
    text_parts = []
    for seg in message:
        if isinstance(seg, dict):
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                text_parts.append(d.get("text", ""))
            elif t == "image":
                text_parts.append("[图片]")
            elif t == "face":
                text_parts.append("[表情]")
            elif t == "record":
                text_parts.append("[语音]")
            elif t == "video":
                text_parts.append("[视频]")
            elif t == "reply":
                if d.get("text"):
                    text_parts.append(f'"{d["text"]}"')
            elif t == "at":
                text_parts.append(f"@{d.get('qq', d.get('name', ''))}")
            else:
                # 其他未知类型也尝试提取文本
                text_parts.append(d.get("text", ""))
    return "".join(text_parts).strip()


# ============ OneBot 协议处理 ============


def make_message_event(message_type: str, user_id: int, message: list,
                       group_id: int = 0, group_name: str = "",
                       nickname: str = "") -> dict:
    """构造 OneBot v11 消息事件"""
    event = {
        "time": int(time.time()),
        "self_id": state._self_id_int,
        "post_type": "message",
    }
    if message_type == "group":
        event["message_type"] = "group"
        event["group_id"] = group_id
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
        event["group_name"] = group_name or str(group_id)
    else:
        event["message_type"] = "private"
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
    return event


def push_event(event: dict) -> bool:
    """通过 WebSocket 客户端连接向 AstrBot 推送事件。"""
    if not state._ob_ws or not state._ob_ws_loop:
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            state._ob_ws.send(json.dumps(event, ensure_ascii=False)),
            state._ob_ws_loop,
        )
        future.result(timeout=5)
        return True
    except Exception as e:
        log.warning(f"[OB11] 推送事件失败: {e}")
        return False


def _decode_base64_image(b64_data: str) -> str | None:
    """在线程池中执行：解码 base64 图片并保存为临时文件。"""
    import tempfile
    img_data = base64.b64decode(b64_data)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(img_data)
    tmp.close()
    return tmp.name
