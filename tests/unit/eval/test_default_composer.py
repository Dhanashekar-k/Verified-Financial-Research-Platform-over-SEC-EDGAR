"""ADR-012: the engine's writer default is haiku; the baseline's recorded runs used sonnet."""

from __future__ import annotations

import pytest

from eval.run import COMPOSERS, default_composer
from eval.systems import SYSTEMS


def test_the_engine_defaults_to_the_writer_adr_012_picked() -> None:
    assert COMPOSERS[default_composer("engine")] == "claude-haiku-5-5"


@pytest.mark.parametrize("system", sorted(set(SYSTEMS) - {"engine"}))
def test_every_other_system_keeps_sonnet(system: str) -> None:
    assert COMPOSERS[default_composer(system)] == "claude-sonnet-5-5"
