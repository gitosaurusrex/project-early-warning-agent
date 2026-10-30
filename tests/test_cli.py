import pytest
from typer.testing import CliRunner

from pewa.cli import app

runner = CliRunner()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    monkeypatch.setenv("PEWA_DB_PATH", str(path))
    monkeypatch.chdir(tmp_path)
    return path


def test_ingest_fixtures_then_snapshot(db):
    result = runner.invoke(app, ["ingest", "--fixtures", "atlas"])
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert "outcomes: 5" in result.output

    result = runner.invoke(app, ["snapshot", "--as-of", "2026-02-13"])
    assert result.exit_code == 0, result.output
    assert "ATL-1" in result.output and "ATL-5" in result.output


def test_ingest_without_credentials_falls_back_to_atlas(db):
    result = runner.invoke(app, ["ingest", "--source", "jira"])
    assert result.exit_code == 0, result.output
    assert "skipping jira: credentials not configured" in result.output
    assert "jira" in result.output


def test_unknown_fixture_set_fails_cleanly(db):
    result = runner.invoke(app, ["ingest", "--fixtures", "nope"])
    assert result.exit_code == 1
    assert "no fixture dataset named 'nope'" in result.output


def test_snapshot_on_empty_db_explains(db):
    result = runner.invoke(app, ["snapshot", "--as-of", "2026-02-13"])
    assert result.exit_code == 1
    assert "pewa ingest" in result.output


def test_init_creates_db_and_env(db, tmp_path):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert (tmp_path / ".env").exists()
