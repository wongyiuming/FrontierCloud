import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SchemaBootstrapContractTests(unittest.TestCase):
    def test_database_startup_is_bootstrap_only(self):
        source = (ROOT / "app/core/db.py").read_text(encoding="utf-8")
        for forbidden in (
            "ALTER TABLE",
            "GET_LOCK(",
            "RELEASE_LOCK(",
            "_column_exists",
            "_column_nullable",
            "_index_exists",
            "_normalize_active_bans",
            "schema-migrations",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("if existing_tables:", source)
        self.assertIn("await _validate_initialized_schema", source)
        self.assertIn("CREATE TABLE frontiercloud_schema", source)
        self.assertIn("FrontierCloud 不支持数据库迁移", source)
        self.assertIn("Schema changes require a new-node initialization", source)

    def test_current_indexes_are_created_in_the_initial_schema(self):
        source = (ROOT / "app/core/db.py").read_text(encoding="utf-8")
        for index in (
            "idx_ip_security_violation_count",
            "idx_media_objects_path",
            "idx_playback_path",
            "idx_playback_event_media",
            "idx_media_lyric_path",
            "idx_lyric_path",
        ):
            self.assertIn(index, source)

    def test_dev_cd_reinitializes_incompatible_schema_instead_of_migrating(self):
        source = (ROOT / "scripts/deploy_rn.sh").read_text(encoding="utf-8")
        self.assertIn("expected_schema_generation", source)
        self.assertIn("performing explicit fresh-node init", source)
        self.assertIn("DROP DATABASE IF EXISTS", source)
        self.assertIn("CREATE DATABASE", source)
        self.assertIn("redis-cli FLUSHDB", source)
        self.assertNotIn("ALTER TABLE", source)


if __name__ == "__main__":
    unittest.main()
