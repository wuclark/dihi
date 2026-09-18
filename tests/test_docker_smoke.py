"""Opt-in smoke tests for the running Docker Compose stack.

Run with: DIHI_DOCKER_TESTS=1 venv/bin/pytest tests/test_docker_smoke.py -v
The Makefile's ``test-docker`` target starts Compose before running these.
"""
import io
import json
import os
import urllib.request
import zipfile

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("DIHI_DOCKER_TESTS") != "1",
    reason="set DIHI_DOCKER_TESTS=1 to run Docker smoke tests",
)

BASE = os.environ.get("DIHI_TEST_URL", "http://localhost:5000").rstrip("/")


def get(path: str, timeout: float = 60):
    # /api/media/library is catalog-backed (slim cards, no per-video
    # description/info.json inline), so it stays within fast-endpoint timing.
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.status, response.headers, response.read()


def test_health_and_library_endpoints():
    status, _, body = get("/health")
    assert status == 200
    assert json.loads(body)["ok"] is True

    status, _, body = get("/api/media/library")
    assert status == 200
    assert isinstance(json.loads(body).get("videos"), list)


def test_catalog_and_extension_download():
    status, _, body = get("/api/media/catalog?page=1&per_page=5")
    assert status == 200
    assert "items" in json.loads(body)

    status, headers, body = get("/extension.zip")
    assert status == 200
    assert headers.get_content_type() == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as bundle:
        assert "manifest.json" in bundle.namelist()


def test_media_route_serves_a_catalog_file():
    _, _, body = get("/api/media/catalog?page=1&per_page=1")
    item = json.loads(body)["items"][0]
    files = item.get("files") or {}
    candidate = next((f for f in files.values() if f.get("kind") in {"video", "audio", "thumbnail"}), None)
    if candidate is None:
        pytest.skip("catalog has no local media file")
    status, _, media = get(candidate["url"])
    assert status == 200
    assert media
