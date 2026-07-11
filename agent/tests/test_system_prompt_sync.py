from collections.abc import Callable
from typing import Any

import pytest
from langfuse.api import NotFoundError, Prompt_Text
from langfuse.model import TextPromptClient

from copilot import observability
from copilot.observability import PromptRef, sync_system_prompt

_NAME = "copilot-system-prompt"
_LABEL = "development"


def _prompt(text: str, version: int) -> TextPromptClient:
    """Build a real TextPromptClient so the sync's isinstance narrowing sees the right type."""
    return TextPromptClient(
        Prompt_Text(
            name=_NAME,
            version=version,
            prompt=text,
            labels=[_LABEL],
            tags=[],
            config={},
            commit_message=None,
        )
    )


class _FakeClient:
    """Records create_prompt calls and replays a scripted get_prompt result."""

    def __init__(self, get_result: TextPromptClient | Exception) -> None:
        self._get_result = get_result
        self.create_calls: list[dict[str, Any]] = []

    def get_prompt(self, name: str, **kwargs: Any) -> TextPromptClient:
        if isinstance(self._get_result, Exception):
            raise self._get_result
        return self._get_result

    def create_prompt(self, **kwargs: Any) -> TextPromptClient:
        self.create_calls.append(kwargs)
        # Server assigns the next version; the current label (if any) is the fetched one.
        return _prompt(kwargs["prompt"], version=99)


# Installs a fake Langfuse client and hands it back for assertions.
_Installer = Callable[[_FakeClient], _FakeClient]


@pytest.fixture
def patch_client(
    monkeypatch: pytest.MonkeyPatch,
) -> _Installer:
    """Swap the module's Langfuse client for a fake; returns an installer for the fake client."""

    def install(client: _FakeClient) -> _FakeClient:
        monkeypatch.setattr(observability, "get_client", lambda: client)
        return client

    return install


def test_disabled_never_touches_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    # Breaks if a Langfuse-unconfigured deploy tries to sync at startup and errors before serving.
    def boom() -> Any:
        raise AssertionError("get_client must not be called when tracing is disabled")

    monkeypatch.setattr(observability, "get_client", boom)
    assert sync_system_prompt(False, _NAME, "prompt", _LABEL) is None


def test_unchanged_prompt_reuses_version_without_creating(patch_client: _Installer) -> None:
    # Breaks if every service restart spawns a redundant prompt version for identical code.
    client = patch_client(_FakeClient(get_result=_prompt("same text", version=4)))
    ref = sync_system_prompt(True, _NAME, "same text", _LABEL)
    assert ref == PromptRef(name=_NAME, version=4)
    assert client.create_calls == []


def test_changed_prompt_creates_new_labeled_version(patch_client: _Installer) -> None:
    # Breaks if editing SYSTEM_PROMPT no longer records a new version in Langfuse.
    client = patch_client(_FakeClient(get_result=_prompt("old text", version=4)))
    ref = sync_system_prompt(True, _NAME, "new text", _LABEL)
    assert ref == PromptRef(name=_NAME, version=99)
    assert len(client.create_calls) == 1
    call = client.create_calls[0]
    assert call["prompt"] == "new text"
    assert call["labels"] == [_LABEL]


def test_missing_prompt_creates_first_version(patch_client: _Installer) -> None:
    # Breaks if the very first sync (no prompt yet) errors instead of creating version 1.
    client = patch_client(_FakeClient(get_result=NotFoundError(body={"message": "not found"})))
    ref = sync_system_prompt(True, _NAME, "first text", _LABEL)
    assert ref == PromptRef(name=_NAME, version=99)
    assert len(client.create_calls) == 1


def test_transient_fetch_error_skips_sync_without_creating(patch_client: _Installer) -> None:
    # Breaks if a flaky fetch both swallows the error AND churns a redundant version each boot,
    # or if it propagates and crashes startup instead of degrading to no sync.
    client = patch_client(_FakeClient(get_result=RuntimeError("network down")))
    ref = sync_system_prompt(True, _NAME, "text", _LABEL)
    assert ref is None
    assert client.create_calls == []
