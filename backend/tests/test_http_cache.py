from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.http_cache import CachePolicyMiddleware
from app import main


class HttpCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "assets").mkdir()
        (self.root / "assets" / "index-abcdefgh.js").write_text("const value = 'hello';\n" * 1000)
        (self.root / "assets" / "unversioned.js").write_text("console.log('hello')")
        (self.root / "index.html").write_text("<html>Hello</html>")

        async def private(request):
            return JSONResponse({"user": "learner", "data": "x" * 3000})

        async def denied(request):
            raise HTTPException(401)

        application = Starlette(routes=[Route("/api/private", private), Route("/api/denied", denied),
            Mount("/", app=GZipMiddleware(StaticFiles(directory=self.root, html=True), minimum_size=1024))],
            middleware=[Middleware(CachePolicyMiddleware)])
        self.client = TestClient(application)

    def test_html_revalidates_and_304_keeps_policy(self):
        first = self.client.get("/")
        self.assertEqual(first.headers["cache-control"], "no-cache")
        second = self.client.get("/", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.headers["cache-control"], "no-cache")

    def test_only_successful_hashed_assets_get_immutable_cache(self):
        for path, expected in [("/assets/index-abcdefgh.js", "public, max-age=31536000, immutable"),
                               ("/assets/unversioned.js", "no-cache"),
                               ("/assets/missing-abcdefgh.js", "no-store")]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).headers["cache-control"], expected)

    def test_static_compression_and_personalized_api_exclusion(self):
        asset = self.client.get("/assets/index-abcdefgh.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(asset.headers["content-encoding"], "gzip")
        self.assertIn("Accept-Encoding", asset.headers["vary"])
        self.assertLess(int(asset.headers["content-length"]), len(asset.content))
        plain = self.client.get("/assets/index-abcdefgh.js", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", plain.headers)
        for path in ("/api/private", "/api/denied", "/api/missing"):
            response = self.client.get(path, headers={"Accept-Encoding": "gzip"})
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("content-encoding", response.headers)

    def test_version_describes_active_build_and_never_caches(self):
        # No lifespan/database initialization is needed for these public requests.
        client = TestClient(main.app)
        with patch.object(main, "STATIC_DIR", self.root):
            for version in ("26.9.28.41", "26.9.28.42"):
                (self.root / "version.json").write_text(json.dumps({"version": version}))
                response = client.get("/api/version")
                self.assertEqual(response.json(), {"version": version})
                self.assertEqual(response.headers["cache-control"], "no-store")
            (self.root / "version.json").unlink()
            self.assertEqual(client.get("/api/version").json(), {"version": None})
            (self.root / "version.json").write_text("invalid")
            self.assertEqual(client.get("/api/version").json(), {"version": None})
        self.assertEqual(client.get("/api/auth/me").headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
