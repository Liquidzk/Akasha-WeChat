import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import state


class StateTests(unittest.TestCase):
    def setUp(self):
        state._ob_id_to_contact.clear()
        state._ob_id_metadata.clear()

    def test_wxid_mapping_is_stable_across_processes(self):
        wxid = "1234567890@chatroom"
        expected = state._wxid_to_int(wxid)
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(BRIDGE_DIR)!r});"
            "import state;"
            f"print(state._wxid_to_int({wxid!r}))"
        )
        actual = int(subprocess.check_output(
            [sys.executable, "-c", script],
            text=True,
        ).strip())
        self.assertEqual(expected, actual)

    def test_route_store_survives_reload_and_blocks_ambiguous_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            route_path = Path(temp_dir) / "routes.json"
            state.init_route_store(str(route_path))
            state.register_contact(
                123,
                "测试群",
                session_id="abc@chatroom",
                kind="group",
                display_name="测试群",
            )
            state.register_contact(
                456,
                "重名群",
                session_id="def@chatroom",
                kind="group",
                display_name="重名群",
                ambiguous=True,
            )

            state._ob_id_to_contact.clear()
            state._ob_id_metadata.clear()
            state.init_route_store(str(route_path))

            self.assertEqual("测试群", state.resolve_contact(123))
            self.assertIsNone(state.resolve_contact(456))
            payload = json.loads(route_path.read_text(encoding="utf-8"))
            self.assertEqual("abc@chatroom", payload["routes"]["123"]["session_id"])


if __name__ == "__main__":
    unittest.main()
