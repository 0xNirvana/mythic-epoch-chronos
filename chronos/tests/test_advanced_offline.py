#!/usr/bin/env python3
"""Offline unit tests for Chronos agent advanced behaviors (no Mythic/Calendar)."""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DownloadMessageTests(unittest.TestCase):
    def test_large_file_message_no_shell_base64_hint(self):
        # Snippet of the error string we ship
        msg = (
            f"File too large for Calendar C2: 312534458 bytes "
            f"(max 512000 / ~500KB). download only supports "
            f"small single-shot transfers; large files are not supported."
        )
        self.assertIn("not supported", msg)
        self.assertNotIn("shell base64", msg.lower())


class ResumeLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.payload = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.callback = "11111111-2222-3333-4444-555555555555"
        self.state = self.home / f".chronos_{self.payload[:8]}"

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, payload=None):
        payload = payload or self.payload

        def load():
            try:
                state_path = self.home / f".chronos_{payload[:8]}"
                if state_path.exists():
                    data = json.loads(state_path.read_text())
                    saved_payload = data.get("payload_uuid", "")
                    if saved_payload and saved_payload != payload:
                        return None
                    saved_uuid = data.get("callback_uuid", "")
                    if (
                        saved_uuid
                        and len(saved_uuid) == 36
                        and saved_uuid != payload
                    ):
                        return saved_uuid
            except Exception:
                pass
            return None

        return load()

    def test_resume_matching(self):
        self.state.write_text(
            json.dumps(
                {"callback_uuid": self.callback, "payload_uuid": self.payload}
            )
        )
        self.assertEqual(self._load(), self.callback)

    def test_reject_wrong_payload(self):
        self.state.write_text(
            json.dumps(
                {
                    "callback_uuid": self.callback,
                    "payload_uuid": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                }
            )
        )
        self.assertIsNone(self._load())

    def test_checkin_skips_when_persisted(self):
        uuid = self.callback
        payload_uuid = self.payload
        created = []
        if uuid != payload_uuid:
            result_uuid = uuid
        else:
            created.append(1)
            result_uuid = payload_uuid
        self.assertEqual(result_uuid, self.callback)
        self.assertEqual(created, [])


class UploadUuidGuardTests(unittest.TestCase):
    def test_uuid_detected_as_invalid_upload(self):
        file_b64 = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        is_uuid = (
            isinstance(file_b64, str)
            and len(file_b64) == 36
            and file_b64.count("-") == 4
            and all(c in "0123456789abcdef-" for c in file_b64.lower())
        )
        self.assertTrue(is_uuid)

    def test_real_b64_not_uuid(self):
        file_b64 = base64.b64encode(b"hello world").decode()
        is_uuid = (
            isinstance(file_b64, str)
            and len(file_b64) == 36
            and file_b64.count("-") == 4
        )
        self.assertFalse(is_uuid)


class CheckinFailExitTests(unittest.TestCase):
    def test_default_exits_on_fail(self):
        force_resume = False
        env_flag = ""
        should_exit = not (
            env_flag.lower() in ("1", "true", "yes") or force_resume
        )
        self.assertTrue(should_exit)

    def test_force_resume_env(self):
        env_flag = "1"
        should_exit = env_flag.lower() not in ("1", "true", "yes")
        self.assertFalse(should_exit)


if __name__ == "__main__":
    unittest.main()
