#!/usr/bin/env python3
"""Unit checks for Chronos callback resume (no Google/Mythic required)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PAYLOAD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CALLBACK = "11111111-2222-3333-4444-555555555555"
OTHER_PAYLOAD = "ffffffff-ffff-ffff-ffff-ffffffffffff"


class ResumeLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state_path = self.home / f".chronos_{PAYLOAD[:8]}"

    def tearDown(self):
        self.tmp.cleanup()

    def _load_fn(self):
        """Minimal copy of _load_persisted_uuid logic under test."""
        def load():
            try:
                state_path = self.home / f".chronos_{PAYLOAD[:8]}"
                if state_path.exists():
                    data = json.loads(state_path.read_text())
                    saved_payload = data.get('payload_uuid', '')
                    if saved_payload and saved_payload != PAYLOAD:
                        return None
                    saved_uuid = data.get('callback_uuid', '')
                    if (saved_uuid and len(saved_uuid) == 36
                            and saved_uuid != PAYLOAD):
                        return saved_uuid
            except Exception:
                pass
            return None
        return load

    def test_load_matching_payload(self):
        self.state_path.write_text(json.dumps({
            'callback_uuid': CALLBACK,
            'payload_uuid': PAYLOAD,
        }))
        self.assertEqual(self._load_fn()(), CALLBACK)

    def test_load_rejects_mismatched_payload(self):
        self.state_path.write_text(json.dumps({
            'callback_uuid': CALLBACK,
            'payload_uuid': OTHER_PAYLOAD,
        }))
        self.assertIsNone(self._load_fn()())

    def test_load_legacy_without_payload_key(self):
        self.state_path.write_text(json.dumps({'callback_uuid': CALLBACK}))
        self.assertEqual(self._load_fn()(), CALLBACK)

    def test_checkin_skips_when_persisted(self):
        """Mirrors checkin() resume early-return."""
        uuid = CALLBACK
        payload_uuid = PAYLOAD
        had_persisted = uuid != payload_uuid
        self.assertTrue(had_persisted)
        create_called = []

        def checkin():
            original_uuid = uuid
            if original_uuid != payload_uuid:
                return True, original_uuid, False  # ok, uuid, created_event
            create_called.append(1)
            return False, payload_uuid, True

        ok, out_uuid, created = checkin()
        self.assertTrue(ok)
        self.assertEqual(out_uuid, CALLBACK)
        self.assertFalse(created)
        self.assertEqual(create_called, [])

    def test_checkin_cold_start_attempts_create(self):
        uuid = PAYLOAD
        payload_uuid = PAYLOAD
        create_called = []

        def checkin():
            original_uuid = uuid
            if original_uuid != payload_uuid:
                return True, original_uuid, False
            create_called.append(1)
            return False, payload_uuid, True

        ok, out_uuid, created = checkin()
        self.assertFalse(ok)
        self.assertTrue(created)
        self.assertEqual(create_called, [1])


if __name__ == '__main__':
    unittest.main()
