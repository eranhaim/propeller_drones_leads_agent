import os
import re
import unittest

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.admin.routes import (
    _require_admin,
    admin_session_secret,
    router as admin_router,
)
from app.config import get_settings


class AdminAuthenticationTests(unittest.TestCase):
    password = "test-admin-password"
    origin = "https://testserver"

    def setUp(self) -> None:
        self.previous_password = os.environ.get("ADMIN_PASSWORD")
        os.environ["ADMIN_PASSWORD"] = self.password
        get_settings.cache_clear()

        app = FastAPI()
        app.add_middleware(
            SessionMiddleware,
            secret_key=admin_session_secret(self.password),
            session_cookie="propeller_admin_session",
            same_site="strict",
            https_only=True,
        )
        app.include_router(admin_router)

        @app.get("/protected")
        async def protected(_: None = Depends(_require_admin)) -> dict:
            return {"ok": True}

        @app.get("/csrf")
        async def csrf_token(
            request: Request,
            _: None = Depends(_require_admin),
        ) -> dict:
            return {"token": request.session["csrf_token"]}

        @app.post("/protected")
        async def protected_post(_: None = Depends(_require_admin)) -> dict:
            return {"ok": True}

        self.client = TestClient(app, base_url=self.origin)

    def tearDown(self) -> None:
        if self.previous_password is None:
            os.environ.pop("ADMIN_PASSWORD", None)
        else:
            os.environ["ADMIN_PASSWORD"] = self.previous_password
        get_settings.cache_clear()

    def _login_form_token(self) -> str:
        response = self.client.get("/admin/login")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def _login(self) -> None:
        response = self.client.post(
            "/admin/login",
            data={
                "password": self.password,
                "csrf_token": self._login_form_token(),
            },
            headers={"Origin": self.origin},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("secure", cookie)

    def test_unauthenticated_requests_redirect_to_login(self) -> None:
        response = self.client.get("/protected", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/login?next=/protected")

    def test_wrong_password_is_denied(self) -> None:
        response = self.client.post(
            "/admin/login",
            data={
                "password": "wrong-password",
                "csrf_token": self._login_form_token(),
            },
            headers={"Origin": self.origin},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/login?error=1")

    def test_correct_password_creates_session_and_csrf_blocks_bad_posts(self) -> None:
        self._login()

        self.assertEqual(self.client.get("/protected").json(), {"ok": True})
        csrf_token = self.client.get("/csrf").json()["token"]

        missing_token = self.client.post(
            "/protected",
            headers={"Origin": self.origin},
            follow_redirects=False,
        )
        self.assertEqual(missing_token.status_code, 403)

        cross_origin = self.client.post(
            "/protected",
            headers={
                "Origin": "https://attacker.example",
                "X-CSRF-Token": csrf_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(cross_origin.status_code, 403)

        valid_post = self.client.post(
            "/protected",
            headers={
                "Origin": self.origin,
                "X-CSRF-Token": csrf_token,
            },
        )
        self.assertEqual(valid_post.json(), {"ok": True})

    def test_every_admin_route_except_login_uses_the_guard(self) -> None:
        for route in admin_router.routes:
            if route.path == "/admin/login":
                continue
            dependencies = [dependency.call for dependency in route.dependant.dependencies]
            self.assertIn(_require_admin, dependencies, route.path)


if __name__ == "__main__":
    unittest.main()
