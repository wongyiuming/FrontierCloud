from __future__ import annotations

import inspect
import unittest


def effective_routes(routes):
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from effective_routes(candidates())
        else:
            yield route


class UploadSiteTypeRuntimeContractTests(unittest.TestCase):
    def test_final_upload_session_schema_cannot_name_storage_member(self):
        from main import app

        routes = [
            route for route in effective_routes(app.routes)
            if getattr(route, "path", None) == "/api/v1/media/admin/upload/session"
            and "POST" in (getattr(route, "methods", None) or set())
        ]
        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route.endpoint.__module__, "app.api.v1.admin_master_mutation_integrity")

        payload_type = inspect.signature(route.endpoint).parameters["payload"].annotation
        fields = getattr(payload_type, "model_fields", {})
        self.assertIn("site_type", fields)
        self.assertNotIn("storage_member_id", fields)
        self.assertTrue(fields["site_type"].is_required())


if __name__ == "__main__":
    unittest.main()
