"""Global test fixtures.

Safety net (CLAUDE.md rule 4): unit and property tests never touch real external
services. Sockets to the Anthropic API and SEC are blocked unless a test is marked
``@pytest.mark.live``. The LLM runs in stub mode by default.
"""

from __future__ import annotations

import socket

import pytest

_BLOCKED_HOSTS = {"api.anthropic.com", "sec.gov", "www.sec.gov", "data.sec.gov"}
_real_create_connection = socket.create_connection


@pytest.fixture(autouse=True)
def _default_llm_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGARQA_LLM_MODE", "stub")


@pytest.fixture(autouse=True)
def _block_external_sockets(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("live"):
        return

    def guarded(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if any(host == h or str(host).endswith("." + h) for h in _BLOCKED_HOSTS):
            raise RuntimeError(
                f"Blocked network call to {host!r} in a non-live test. "
                f"Mark it @pytest.mark.live to allow."
            )
        return _real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)
