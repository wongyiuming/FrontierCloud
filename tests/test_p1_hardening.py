import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class P1HardeningTests(unittest.TestCase):
    def test_runtime_base_images_are_patch_pinned(self):
        web_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        updater_dockerfile = (ROOT / "updater" / "Dockerfile").read_text(encoding="utf-8")
        nginx_dockerfile = (ROOT / "nginx" / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.14.7-slim", web_dockerfile)
        self.assertIn("FROM python:3.14.7-alpine", updater_dockerfile)
        self.assertIn("FROM nginx:1.30.4-alpine", nginx_dockerfile)
        self.assertIn("image: redis:7.4.11-alpine", compose)
        self.assertIn("image: mysql:8.4.11", compose)
        self.assertIn("image: coturn/coturn:4.17.2-r0-alpine", compose)
        self.assertNotIn("image: redis:7-alpine", compose)
        self.assertNotIn("image: mysql:8.4\n", compose)

    def test_web_has_read_only_release_control_mount(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        updater = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
        web = compose.split("  web:\n", 1)[1].split("\n  redis:\n", 1)[0]

        self.assertIn("updater_control:/run/frontiercloud-updater\n", updater)
        self.assertNotIn("updater_control:/run/frontiercloud-updater:ro", updater)
        self.assertIn("updater_control:/run/frontiercloud-updater:ro", web)


if __name__ == "__main__":
    unittest.main()
