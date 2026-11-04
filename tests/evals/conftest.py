import pytest


@pytest.fixture(autouse=True)
def own_session_ledger(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a session ledger of its own: this machine's real one holds the live drivers' sessions."""
    monkeypatch.setenv('EVALS_SESSION_LEDGER', str(tmp_path / 'session-ledger'))
