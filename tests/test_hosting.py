"""Hosting: the dashboard password, the public-bind guard, the demo seed and the Render Blueprint."""

import base64

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pewa.cli import app
from pewa.config import REPO_ROOT
from pewa.db import make_engine
from pewa.web.app import create_app, password_ok

runner = CliRunner()


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.mark.parametrize("header, ok", [
    (basic("anyone", "hunter2"), True),
    (basic("", "hunter2"), True),
    (basic("anyone", "hunter3"), False),
    (basic("hunter2", ""), False),
    ("Bearer hunter2", False),
    ("Basic not-base64!", False),
    (None, False),
])
def test_password_check(header, ok):
    assert password_ok(header, "hunter2") is ok


def test_password_protects_every_page_but_the_health_check(tmp_path):
    client = TestClient(create_app(make_engine(tmp_path / "h.db"), project="atlas", dry_run=True, password="hunter2"))
    assert client.get("/healthz").json() == {"ok": True}
    for path in ("/", "/reports", "/backtests", "/events/evt_x"):
        response = client.get(path)
        assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Basic")
    assert client.post("/runs", data={"as_of": "2026-02-13"}).status_code == 401  # no model calls either
    assert client.get("/", headers={"Authorization": basic("me", "hunter2")}).status_code == 200


def test_no_password_means_no_prompt(tmp_path):
    client = TestClient(create_app(make_engine(tmp_path / "h.db"), project="atlas", dry_run=True))
    assert client.get("/").status_code == 200


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PEWA_DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_serve_refuses_a_public_bind_without_a_password(db):
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1 and "without a password" in result.output


def test_serve_reads_port_from_the_environment(db, monkeypatch):
    calls = {}
    monkeypatch.setattr("uvicorn.run", lambda app_, **kw: calls.update(kw))
    monkeypatch.setenv("PEWA_DASHBOARD_PASSWORD", "hunter2")
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"], env={"PORT": "10000"})
    assert result.exit_code == 0, result.output
    assert calls["host"] == "0.0.0.0" and calls["port"] == 10000 and "password required" in result.output


def test_demo_seeds_once_and_never_calls_the_model(db, monkeypatch):
    monkeypatch.setenv("PEWA_ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    result = runner.invoke(app, ["demo"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "1023 events" in result.output and "backtest 1" in result.output
    assert "already loaded" in runner.invoke(app, ["demo"]).output

    client = TestClient(create_app(make_engine(db / "demo.db"), project="atlas"))
    overview = client.get("/").text
    assert "trend vs. 06 Feb" in overview and "dry-run" in overview
    assert "Backtest 1 (demo baseline)" in client.get("/backtests").text


def test_render_blueprint_matches_the_cli():
    service = yaml.safe_load((REPO_ROOT / "render.yaml").read_text())["services"][0]
    assert service["type"] == "web" and service["runtime"] == "python" and service["healthCheckPath"] == "/healthz"
    assert service["startCommand"] == ".venv/bin/pewa demo && .venv/bin/pewa serve --host 0.0.0.0"
    env = {v["key"]: v for v in service["envVars"]}
    assert env["PEWA_DASHBOARD_PASSWORD"] == {"key": "PEWA_DASHBOARD_PASSWORD", "generateValue": True}
    assert env["PEWA_ANTHROPIC_API_KEY"].get("sync") is False  # a secret: never committed
