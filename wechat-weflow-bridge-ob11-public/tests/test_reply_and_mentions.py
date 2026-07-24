import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import config
import ob_protocol
import state
from bridge_core import WeFlowBridge, _is_bot_mentioned, _strip_bot_mentions


class FakeSender:
    pass


class ReplyAndMentionTests(unittest.TestCase):
    def setUp(self):
        with ob_protocol._message_cache_lock:
            ob_protocol._message_cache.clear()

    def test_wechat_thin_space_mention_is_recognized_and_removed(self):
        with patch.object(config, "BOT_NICKNAMES", ["Bot"]):
            content = "@Bot\u2005/排行榜"
            self.assertTrue(_is_bot_mentioned(content))
            self.assertEqual("/排行榜", _strip_bot_mentions(content))

    def test_similar_name_is_not_treated_as_bot_mention(self):
        with patch.object(config, "BOT_NICKNAMES", ["Bot"]):
            self.assertFalse(_is_bot_mentioned("@Botany 你好"))

    def test_quote_snapshot_is_cached_for_onebot_get_msg(self):
        bridge = WeFlowBridge(FakeSender())
        group_id = state._wxid_to_int("test@chatroom")
        entry = {
            "session_id_data": "test@chatroom",
            "group_name": "测试群",
            "reply": {
                "message_id": "6116895530414915131",
                "quote": {
                    "sender": "wxid_original",
                    "accountName": "原发送者",
                    "content": "原消息内容",
                },
            },
        }

        segment = bridge._make_reply_segment(entry, True, group_id)

        self.assertEqual(
            {"type": "reply", "data": {"id": "6116895530414915131"}},
            segment,
        )
        cached = ob_protocol.get_cached_message("6116895530414915131")
        self.assertIsNotNone(cached)
        self.assertEqual(group_id, cached["group_id"])
        self.assertEqual("原发送者", cached["sender"]["nickname"])
        self.assertEqual(
            [{"type": "text", "data": {"text": "原消息内容"}}],
            cached["message"],
        )


if __name__ == "__main__":
    unittest.main()
