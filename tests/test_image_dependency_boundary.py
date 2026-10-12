"""Keep vendor test keys out of every layer without deleting runtime modules."""
import pathlib
import re
import shlex
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


def dependency_cleanup():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    logical = re.sub(r"\\\r?\n", " ", dockerfile)
    runs = [line.strip() for line in logical.splitlines() if line.startswith("RUN ")]
    dependency = [line for line in runs if "python -m pip install " in line]
    if len(dependency) != 1 or "shutil.rmtree" not in dependency[0]:
        raise AssertionError("Vendor fixtures must be removed in the pip installation RUN")
    cleanup_layers = [line for line in runs if "shutil.rmtree" in line]
    if cleanup_layers != dependency:
        raise AssertionError("Later deletion leaves private fixtures in image history")
    tokens = shlex.split(dependency[0])
    cleanup = [tokens[index + 2] for index in range(len(tokens) - 2)
               if tokens[index:index + 2] == ["python", "-c"]
               and "shutil.rmtree" in tokens[index + 2]]
    if len(cleanup) != 1:
        raise AssertionError("Expected one explicit ACME test-directory cleanup")
    return compile(cleanup[0], "<Dockerfile dependency cleanup>", "exec")


class ImageDependencyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fc-image-boundary-")
        self.root = pathlib.Path(self.directory.name).resolve()
        self.site = self.root / "site-packages"
        self.target = self.site / "acme" / "_internal" / "tests"
        self.target.mkdir(parents=True)
        self.cleanup = dependency_cleanup()
        self.runtime = self.site / "acme" / "client.py"
        self.runtime.write_text("ACME runtime client", encoding="utf-8")
        self.internal = self.target.parent / "main.py"
        self.internal.write_text("ACME runtime internals", encoding="utf-8")
        self.other = self.site / "another-package" / "tests" / "fixture.txt"
        self.other.parent.mkdir(parents=True)
        self.other.write_text("unrelated package", encoding="utf-8")
        self.project = self.root / "project" / "tests" / "test_pairing.py"
        self.project.parent.mkdir(parents=True)
        self.project.write_text("project test", encoding="utf-8")
        self.script = self.root / "project" / "scripts" / "operator.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("operator script", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def execute(self):
        with patch("sysconfig.get_path", return_value=str(self.site)) as config:
            exec(self.cleanup, {})
            config.assert_called_once_with("purelib")

    def test_installation_layer_removes_only_installed_acme_tests(self):
        (self.target / "testdata").mkdir()
        (self.target / "testdata" / "fixture.pem").write_text("vendor fixture", encoding="utf-8")
        self.execute()
        self.assertFalse(self.target.exists())
        for path in (self.runtime, self.internal, self.other, self.project, self.script):
            with self.subTest(path=path):
                self.assertTrue(path.is_file())

    def test_vendor_tests_missing_is_safe_and_idempotent(self):
        self.target.rmdir()
        self.execute()
        self.execute()
        self.assertTrue(self.runtime.is_file())
        self.assertTrue(self.other.is_file())

    def test_unexpected_file_in_place_of_tests_refuses_deletion(self):
        self.target.rmdir()
        self.target.write_text("unexpected packaging", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "Unexpected ACME test fixture path"):
            self.execute()
        self.assertTrue(self.target.is_file())
        self.assertTrue(self.runtime.is_file())

    def test_resolved_path_outside_installed_package_refuses_deletion(self):
        original = pathlib.Path.resolve
        outside = self.root / "outside"
        outside.mkdir()
        protected = outside / "protected.txt"
        protected.write_text("preserve", encoding="utf-8")

        def resolve(path, *args, **kwargs):
            return outside if path == self.target else original(path, *args, **kwargs)

        with patch.object(pathlib.Path, "resolve", resolve):
            with self.assertRaises(ValueError):
                self.execute()
        self.assertTrue(protected.is_file())
        self.assertTrue(self.target.is_dir())

    def test_symlink_inside_site_packages_cannot_redirect_cleanup(self):
        original = pathlib.Path.resolve

        def resolve(path, *args, **kwargs):
            return self.other.parent if path == self.target else original(path, *args, **kwargs)

        with patch.object(pathlib.Path, "resolve", resolve):
            with self.assertRaisesRegex(AssertionError, "escaped installed package"):
                self.execute()
        self.assertTrue(self.other.is_file())
        self.assertTrue(self.target.is_dir())

    def test_project_tests_still_enter_product_image(self):
        self.assertIn("COPY tests ./tests", (ROOT / "Dockerfile").read_text(encoding="utf-8"))

    def test_cleanup_moved_to_later_run_is_rejected(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        pip_line = "    python -m pip install --no-cache-dir -r /tmp/requirements.txt && \\\n"
        later = dockerfile.replace(pip_line,
            "    python -m pip install --no-cache-dir -r /tmp/requirements.txt\n")
        later = later.replace('    python -c "import pathlib,shutil,sysconfig;',
            'RUN python -c "import pathlib,shutil,sysconfig;')
        self.assertNotEqual(later, dockerfile)
        with patch.object(pathlib.Path, "read_text", return_value=later):
            with self.assertRaisesRegex(AssertionError, "pip installation RUN"):
                dependency_cleanup()


if __name__ == "__main__":
    unittest.main()
