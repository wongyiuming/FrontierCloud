from __future__ import annotations

import unittest


def effective_routes(routes):
    """Flatten preserved include-router trees for runtime surface assertions."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from effective_routes(candidates())
        else:
            yield route


class ComputeRetirementRuntimeTests(unittest.TestCase):
    def test_worker_control_routes_are_not_mounted(self):
        from main import app

        paths = {
            str(getattr(route, "path", ""))
            for route in effective_routes(app.routes)
        }
        self.assertFalse(
            any(path.startswith("/internal/v1/jobs/") for path in paths),
            sorted(path for path in paths if "/jobs/" in path),
        )
        self.assertIn("/internal/v1/identity", paths)
        self.assertIn("/internal/v1/heartbeat", paths)


if __name__ == "__main__":
    unittest.main()
