import unittest

import main
from app.services import admin_service


class DocumentationAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def test_all_documentation_routes_require_existing_admin_session(self):
        protected_paths = {"/docs", "/redoc", "/openapi.json"}
        routes = {
            route.path: route
            for route in main.app.routes
            if getattr(route, "path", None) in protected_paths
        }

        self.assertEqual(set(routes), protected_paths)
        for route in routes.values():
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(admin_service.require_admin, dependencies)

    def test_fastapi_default_public_documentation_routes_are_disabled(self):
        self.assertIsNone(main.app.docs_url)
        self.assertIsNone(main.app.redoc_url)
        self.assertIsNone(main.app.openapi_url)

    async def test_docs_reference_the_protected_schema_route(self):
        swagger = await main.protected_docs("session")
        redoc = await main.protected_redoc("session")

        self.assertIn('/openapi.json', swagger.body.decode("utf-8"))
        self.assertIn('/openapi.json', redoc.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
