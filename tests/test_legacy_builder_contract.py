import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LegacyDockerBuilderContractTests(unittest.TestCase):
    def test_runtime_release_dockerfiles_do_not_require_buildkit(self):
        for relative in ("Dockerfile", "nginx/Dockerfile"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("COPY --chmod=", source, relative)
            self.assertNotIn("RUN --mount=", source, relative)
            self.assertNotIn("COPY --link", source, relative)

    def test_updater_bootstrap_dockerfile_stays_legacy_compatible(self):
        source = (ROOT / "updater" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY --chmod=", source)
        self.assertNotIn("RUN --mount=", source)
        self.assertNotIn("COPY --link", source)


if __name__ == "__main__":
    unittest.main()
