"""Shared helpers for FTC source tests (distinct module name to avoid import clashes).

Fixtures under ``fixtures/`` are real DRF responses (``f16947.xml`` full; ``x*.xml`` trimmed
excerpts of the 이유 section). Provenance: ``fixtures/prov.json``.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from jettae.sources.ftc.parse import FtcDecision, parse_decision

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@cache
def decision(name: str) -> FtcDecision:
    return parse_decision(fixture_bytes(name))
