import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SchemaBootstrapContractTests(unittest.TestCase):
    def test_database_startup_supports_versioned_migrations(self):
        source = (ROOT / "app/core/db.py").read_text(encoding="utf-8")
        migrations = (ROOT / "app/core/schema_migrations.py").read_text(encoding="utf-8")

        self.assertIn("CURRENT_SCHEMA_GENERATION", source)
        self.assertIn("await migrate_schema", source)
        self.assertIn("frontiercloud_schema_migrations", migrations)
        self.assertIn("GET_LOCK(", migrations)
        self.assertIn("RELEASE_LOCK(", migrations)
        self.assertIn("column_exists", migrations)
        self.assertIn("index_exists", migrations)
        self.assertIn("add_column_if_missing", migrations)
        self.assertIn("add_index_if_missing", migrations)
        self.assertIn("generation marker 未推进", migrations)
        self.assertIn("不支持对无代际标记数据库自动迁移", source)
        self.assertIn("禁止旧版本应用启动或降级", source + migrations)

    def test_migration_chain_is_explicit_and_contiguous(self):
        from app.core import schema_migrations

        schema_migrations.validate_migration_registry()
        self.assertGreaterEqual(schema_migrations.CURRENT_SCHEMA_GENERATION, 2)
        self.assertEqual(
            set(schema_migrations.MIGRATIONS),
            set(range(
                schema_migrations.BASE_SCHEMA_GENERATION + 1,
                schema_migrations.CURRENT_SCHEMA_GENERATION + 1,
            )),
        )

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


if __name__ == "__main__":
    unittest.main()
