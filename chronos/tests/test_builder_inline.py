#!/usr/bin/env python3
"""Tests for builder protocol inlining (no Mythic container required)."""
import ast
import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AGENT_CODE = _REPO / "chronos" / "agent_code"
sys.path.insert(0, str(_AGENT_CODE.parent.parent / "chronos" / "mythic" / "agent_functions"))

# Minimal copy of builder inline logic to avoid Mythic imports in CI.
def _prepare_protocol_for_inline(protocol_src: str) -> str:
    out = []
    for line in protocol_src.splitlines():
        if line.strip() == "from __future__ import annotations":
            continue
        out.append(line)
    return "\n".join(out)


def _inline_protocol_v2(agent_code: str, protocol_src: str) -> str:
    protocol_src = _prepare_protocol_for_inline(protocol_src)
    lines = agent_code.splitlines()
    out = []
    skipping = False
    for line in lines:
        if line.strip().startswith("from protocol_v2 import"):
            skipping = True
            out.append("# --- protocol_v2 (inlined at build) ---")
            out.extend(protocol_src.splitlines())
            continue
        if skipping:
            if line.strip() == ")":
                skipping = False
            continue
        out.append(line)
    return "\n".join(out)


class BuilderInlineTests(unittest.TestCase):
    def test_inlined_agent_compiles(self):
        agent_path = _AGENT_CODE / "chronos_payload.py"
        protocol_path = _AGENT_CODE / "protocol_v2.py"
        agent_code = agent_path.read_text()
        protocol_src = protocol_path.read_text()
        combined = _inline_protocol_v2(agent_code, protocol_src)
        # Must parse without SyntaxError (__future__ mid-file was the bug).
        ast.parse(combined)
        self.assertNotIn(
            "from __future__ import annotations",
            combined.split("# --- protocol_v2", 1)[-1],
        )


if __name__ == "__main__":
    unittest.main()
