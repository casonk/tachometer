import pytest

pytest_plugins = ["dyno_lab.fixtures"]


@pytest.fixture(autouse=True)
def _isolated_portfolio_site(tmp_path_factory, monkeypatch):
    """Resolve the portfolio site from an empty store, never this machine's real one."""
    monkeypatch.setenv("PORTFOLIO_STORE", str(tmp_path_factory.mktemp("portfolio-store")))
    monkeypatch.delenv("PORTFOLIO_SITE_DIR", raising=False)
    monkeypatch.delenv("PORTFOLIO_ALLOWED_HOSTS", raising=False)
