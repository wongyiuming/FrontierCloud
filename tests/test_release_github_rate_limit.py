"""Regression coverage for authenticated GitHub release verification and rate-limit handling."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.services import release_control


MAIN_SHA = "a" * 40
SOURCE_SHA = "b" * 40
TREE_SHA = "c" * 40


class StubResponse:
    def __init__(self, payload, *, status_code: int = 200, headers: dict[str, str] | None = None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", "https://api.github.com/test")
        response = httpx.Response(
            self.status_code,
            headers=self.headers,
            json=self.payload,
            request=request,
        )
        raise httpx.HTTPStatusError(
            f"HTTP {self.status_code}",
            request=request,
            response=response,
        )

    def json(self):
        return self.payload


class StubClient:
    def __init__(self, *, branch: StubResponse, pulls=None, runs=None, source=None):
        self.branch = branch
        self.pulls = StubResponse(pulls if pulls is not None else [promotion_pr()])
        self.runs = StubResponse(runs if runs is not None else {"workflow_runs": [dev_run()]})
        self.source = StubResponse(source if source is not None else source_payload())
        self.requests: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, params: dict | None = None):
        self.requests.append((url, params))
        if url == release_control.BRANCH_URL:
            return self.branch
        if url == f"{release_control.REPOSITORY_API}/commits/{MAIN_SHA}/pulls":
            return self.pulls
        if url == release_control.CI_URL:
            return self.runs
        if url == f"{release_control.REPOSITORY_API}/commits/{SOURCE_SHA}":
            return self.source
        raise AssertionError(f"unexpected request: {url}")


def branch_payload() -> dict:
    return {
        "commit": {
            "sha": MAIN_SHA,
            "commit": {"tree": {"sha": TREE_SHA}},
        }
    }


def promotion_pr() -> dict:
    return {
        "merged_at": "2026-09-26T00:00:00Z",
        "base": {"ref": "main"},
        "head": {
            "ref": "dev",
            "sha": SOURCE_SHA,
            "repo": {"full_name": "wongyiuming/FrontierCloud"},
        },
    }


def source_payload() -> dict:
    return {"commit": {"tree": {"sha": TREE_SHA}}}


def dev_run() -> dict:
    return {
        "head_branch": "dev",
        "event": "push",
        "head_sha": SOURCE_SHA,
        "status": "completed",
        "conclusion": "success",
        "run_number": 570,
        "html_url": "https://example.invalid/run/570",
        "updated_at": "2026-09-26T00:00:00Z",
    }


class GitHubReleaseVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        release_control._ci_cache = (0.0, {})
        release_control._ci_last_verified = {}
        release_control._github_backoff_until = 0.0

    def tearDown(self):
        release_control._ci_cache = (0.0, {})
        release_control._ci_last_verified = {}
        release_control._github_backoff_until = 0.0

    async def test_configured_token_is_sent_as_bearer_auth(self):
        client = StubClient(branch=StubResponse(branch_payload()))
        factory = MagicMock(return_value=client)
        with (
            patch.object(release_control.settings, "GITHUB_API_TOKEN", "github-read-token"),
            patch.object(release_control.httpx, "AsyncClient", factory),
        ):
            result = await release_control.ci_status(force=True)

        self.assertTrue(result["publishable"])
        self.assertTrue(result["authenticated"])
        headers = factory.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer github-read-token")

    async def test_primary_rate_limit_enters_backoff_and_force_refresh_does_not_hammer(self):
        reset_at = int(time.time()) + 300
        response = StubResponse(
            {"message": "API rate limit exceeded"},
            status_code=403,
            headers={
                "x-ratelimit-limit": "60",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-used": "60",
                "x-ratelimit-reset": str(reset_at),
            },
        )
        client = StubClient(branch=response)
        factory = MagicMock(return_value=client)
        with (
            patch.object(release_control.settings, "GITHUB_API_TOKEN", ""),
            patch.object(release_control.httpx, "AsyncClient", factory),
        ):
            first = await release_control.ci_status(force=True)
            second = await release_control.ci_status(force=True)

        self.assertFalse(first["publishable"])
        self.assertEqual(first["error_kind"], "rate_limited")
        self.assertEqual(first["http_status"], 403)
        self.assertEqual(first["rate_limit"]["remaining"], 0)
        self.assertEqual(first["rate_limit"]["limit"], 60)
        self.assertGreaterEqual(first["retry_after_seconds"], 298)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(second["error_kind"], "rate_limited")
        self.assertLessEqual(second["retry_after_seconds"], first["retry_after_seconds"])

    async def test_rate_limit_keeps_last_verified_for_display_but_never_for_upgrade(self):
        good_client = StubClient(branch=StubResponse(branch_payload()))
        with (
            patch.object(release_control.settings, "GITHUB_API_TOKEN", ""),
            patch.object(release_control.httpx, "AsyncClient", return_value=good_client),
        ):
            verified = await release_control.ci_status(force=True)
        self.assertTrue(verified["publishable"])

        release_control._ci_cache = (0.0, {})
        release_control._github_backoff_until = 0.0
        limited = StubResponse(
            {"message": "API rate limit exceeded"},
            status_code=403,
            headers={
                "x-ratelimit-limit": "60",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(time.time()) + 120),
            },
        )
        with (
            patch.object(release_control.settings, "GITHUB_API_TOKEN", ""),
            patch.object(release_control.httpx, "AsyncClient", return_value=StubClient(branch=limited)),
        ):
            result = await release_control.ci_status(force=True)

        self.assertFalse(result["publishable"])
        self.assertNotIn("sha", result)
        self.assertEqual(result["last_verified"]["sha"], MAIN_SHA)
        self.assertTrue(result["last_verified"]["publishable"])


if __name__ == "__main__":
    unittest.main()
