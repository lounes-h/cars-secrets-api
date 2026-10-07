import tomllib
from pathlib import Path


def test_health_checks_the_database(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_reports_the_released_version(client):
    # The release bumps pyproject.toml; the API must not keep a version of its own.
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    info = client.get("/api/openapi.json").json()["info"]
    assert info["version"] == pyproject["project"]["version"]
