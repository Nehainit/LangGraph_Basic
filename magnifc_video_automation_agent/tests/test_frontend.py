"""The studio and its local assets must be reachable from the application."""

from fastapi.testclient import TestClient

from video_automation.backend import app


def test_studio_assets_are_served_without_exposing_project_files():
    client = TestClient(app)
    for path, content_type in (
        ("/", "text/html"),
        ("/static/studio.css", "text/css"),
        ("/static/studio.js", "javascript"),
        ("/examples/robot-story-poster.jpg", "image/jpeg"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert content_type in response.headers["content-type"]

    for path in ("/static/.env", "/static/%2e%2e/.env", "/examples/%2e%2e/.env"):
        assert client.get(path).status_code == 404
