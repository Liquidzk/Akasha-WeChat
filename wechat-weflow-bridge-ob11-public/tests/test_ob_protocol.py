import sys
import unittest
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import ob_protocol
import state


class FakeSender:
    def __init__(self):
        self.text_calls = []

    def send_text(self, contact, text):
        self.text_calls.append((contact, text))
        return True

    def send_image(self, contact, image_path):
        return True


class OneBotProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state._ob_id_to_contact.clear()
        state._ob_id_metadata.clear()
        self.sender = FakeSender()
        state.sender_instance = self.sender

    async def test_send_msg_uses_group_id_when_present(self):
        state.register_contact(
            123,
            "燕云测试群",
            session_id="group@chatroom",
            kind="group",
            display_name="燕云测试群",
        )
        response = await ob_protocol._handle_send_api(
            "send_msg",
            {
                "group_id": 123,
                "message": [{"type": "text", "data": {"text": "测试"}}],
            },
        )
        self.assertEqual("ok", response["status"])
        self.assertEqual([("燕云测试群", "测试")], self.sender.text_calls)

    async def test_unknown_route_fails_without_sending(self):
        response = await ob_protocol._handle_send_api(
            "send_private_msg",
            {
                "user_id": 999,
                "message": [{"type": "text", "data": {"text": "不应发送"}}],
            },
        )
        self.assertEqual("failed", response["status"])
        self.assertEqual([], self.sender.text_calls)

    async def test_ambiguous_route_fails_without_sending(self):
        state.register_contact(
            456,
            "重名群",
            session_id="duplicate@chatroom",
            kind="group",
            display_name="重名群",
            ambiguous=True,
        )
        response = await ob_protocol._handle_send_api(
            "send_group_msg",
            {
                "group_id": 456,
                "message": [{"type": "text", "data": {"text": "不应发送"}}],
            },
        )
        self.assertEqual(1409, response["retcode"])
        self.assertEqual([], self.sender.text_calls)

    async def test_unsendable_route_requires_contact_override(self):
        state.register_contact(
            789,
            "789@chatroom",
            session_id="789@chatroom",
            kind="group",
            display_name="789@chatroom",
            sendable=False,
        )
        response = await ob_protocol._handle_send_api(
            "send_group_msg",
            {
                "group_id": 789,
                "message": [{"type": "text", "data": {"text": "不应发送"}}],
            },
        )
        self.assertEqual(1409, response["retcode"])
        self.assertIn("contact_overrides", response["message"])
        self.assertEqual([], self.sender.text_calls)


if __name__ == "__main__":
    unittest.main()
