import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import config
import main
import state
from bridge_core import _route_name_is_ambiguous


class FakeResponse:
    def __init__(self, sessions):
        self._sessions = sessions

    def raise_for_status(self):
        return None

    def json(self):
        return {"sessions": self._sessions}


class RouteAmbiguityTests(unittest.TestCase):
    def setUp(self):
        state._ob_id_to_contact.clear()
        state._ob_id_metadata.clear()
        self._original_route_file = state._route_file
        self._temp_dir = tempfile.TemporaryDirectory()
        state.init_route_store(
            str(Path(self._temp_dir.name) / "routes.json")
        )

    def tearDown(self):
        state._ob_id_to_contact.clear()
        state._ob_id_metadata.clear()
        state._route_file = self._original_route_file
        self._temp_dir.cleanup()

    def test_group_and_private_with_same_name_are_sendable(self):
        sessions = [
            {
                "username": "group@chatroom",
                "displayName": "旧群名",
                "sessionType": "group",
            },
            {
                "username": "private-wxid",
                "displayName": "Liquid",
                "sessionType": "private",
            },
        ]
        with (
            patch.object(
                config,
                "CONTACT_OVERRIDES",
                {"group@chatroom": "Liquid"},
            ),
            patch.object(
                main.requests,
                "get",
                return_value=FakeResponse(sessions),
            ),
        ):
            main._preload_weflow_routes()

        group_id = state._wxid_to_int("group@chatroom")
        private_id = state._wxid_to_int("private-wxid")
        self.assertEqual("Liquid", state.resolve_contact(group_id))
        self.assertEqual("Liquid", state.resolve_contact(private_id))

    def test_two_groups_with_same_name_remain_blocked(self):
        first_id = state._wxid_to_int("first@chatroom")
        second_id = state._wxid_to_int("second@chatroom")
        state.register_contact(
            first_id,
            "重名群",
            session_id="first@chatroom",
            kind="group",
        )
        state.register_contact(
            second_id,
            "重名群",
            session_id="second@chatroom",
            kind="group",
        )

        self.assertTrue(
            _route_name_is_ambiguous("group", first_id, "重名群")
        )
        self.assertTrue(
            _route_name_is_ambiguous("group", second_id, "重名群")
        )


if __name__ == "__main__":
    unittest.main()
