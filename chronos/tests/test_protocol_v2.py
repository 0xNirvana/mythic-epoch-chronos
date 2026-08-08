#!/usr/bin/env python3
"""Unit tests for Protocol V2 pack/unpack and full-payload event HMAC."""

import os
import sys
import unittest

# Allow imports from agent_code (dev tree) or repo shared/
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_AGENT_CODE = os.path.join(_REPO, "chronos", "agent_code")
_SHARED = os.path.join(_REPO, "..", "shared")
for _p in (_AGENT_CODE, _SHARED, os.path.join(_REPO, "..")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from protocol_v2 import (  # noqa: E402
    MAX_DESCRIPTION_BYTES,
    MAX_EXT_PROP_VALUE,
    MAX_LOCATION_BYTES,
    pack_event_fields,
    sign_event_payload,
    unpack_event_fields,
    verify_event_sig,
)


class PackUnpackTests(unittest.TestCase):
    def test_roundtrip_small(self):
        data = "a" * 100
        desc, loc, chunks = pack_event_fields(data)
        event = {
            "description": desc,
            "location": loc,
            "extendedProperties": {"private": chunks},
        }
        self.assertEqual(unpack_event_fields(event), data)

    def test_roundtrip_overflow(self):
        size = MAX_DESCRIPTION_BYTES + MAX_LOCATION_BYTES + (MAX_EXT_PROP_VALUE * 3) + 50
        data = "Z" * size
        desc, loc, chunks = pack_event_fields(data)
        event = {
            "description": desc,
            "location": loc,
            "extendedProperties": {"private": chunks},
        }
        self.assertEqual(unpack_event_fields(event), data)

    def test_server_ingress_parity(self):
        """Simulate agent checkin payload spanning description + location."""
        data = "B" * (MAX_DESCRIPTION_BYTES + 500)
        desc, loc, chunks = pack_event_fields(data)
        # Old bug: server read description only
        self.assertGreater(len(loc), 0)
        truncated = desc
        self.assertNotEqual(truncated, data)
        event = {
            "description": desc,
            "location": loc,
            "extendedProperties": {"private": chunks},
        }
        self.assertEqual(unpack_event_fields(event), data)


class EventHmacTests(unittest.TestCase):
    def setUp(self):
        self.key = b"\x01" * 32

    def _event(self, desc, loc="", chunks=None):
        chunks = chunks or {}
        props = dict(chunks)
        props["event_sig"] = sign_event_payload(self.key, desc, loc, chunks)
        return {
            "description": desc,
            "location": loc,
            "extendedProperties": {"private": props},
        }

    def test_verify_description_only(self):
        event = self._event("hello")
        self.assertTrue(verify_event_sig(event, self.key))

    def test_verify_all_fields(self):
        desc, loc, chunks = pack_event_fields("x" * (MAX_DESCRIPTION_BYTES + 100))
        event = self._event(desc, loc, chunks)
        self.assertTrue(verify_event_sig(event, self.key))

    def test_tamper_location_fails(self):
        desc, loc, chunks = pack_event_fields("y" * (MAX_DESCRIPTION_BYTES + 10))
        event = self._event(desc, loc, chunks)
        event["location"] = "tampered"
        self.assertFalse(verify_event_sig(event, self.key))

    def test_tamper_chunk_fails(self):
        data = "c" * (MAX_DESCRIPTION_BYTES + MAX_LOCATION_BYTES + MAX_EXT_PROP_VALUE + 5)
        desc, loc, chunks = pack_event_fields(data)
        event = self._event(desc, loc, chunks)
        props = event["extendedProperties"]["private"]
        props["chunk_0"] = props["chunk_0"][:-1] + "X"
        self.assertFalse(verify_event_sig(event, self.key))


if __name__ == "__main__":
    unittest.main()
