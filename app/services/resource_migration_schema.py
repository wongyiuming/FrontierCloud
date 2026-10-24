"""Python generation-5 migration journal schema.

Keep this generation's definitions stable after publication. Later schema
changes require a new migration signature, not edits to published history.
This journal is independent of the retired compute-job tables.
"""
import json

from sqlalchemy import BigInteger, CheckConstraint, Column, Index, Integer, JSON, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

metadata = MetaData()


def ident(name, length=32, *, primary=False):
    return Column(name, String(length), primary_key=primary, nullable=False)


def number(name, *, default=None):
    return Column(name, BigInteger, nullable=False, server_default=default)


def check(name, expression):
    return CheckConstraint(expression, name='chk_' + name)


tasks = Table('migration_tasks', metadata,
    ident('task_id', primary=True), ident('master_id'), ident('source_id'), ident('target_id'),
    ident('source_relationship'), ident('target_relationship'), ident('plan_sha256', 64),
    Column('plan', JSON, nullable=False), ident('state'), number('revision', default='1'),
    number('object_count'), number('total_bytes'), number('committed_bytes', default='0'),
    Column('error_code', String(64), nullable=False, server_default=''),
    Column('resume_state', String(32), nullable=False, server_default=''),
    Column('pause_reason', String(16), nullable=False, server_default=''),
    number('created_at'), number('updated_at'),
    check('migration_task_members', 'source_id <> target_id'),
    check('migration_task_counts', 'object_count >= 0 AND total_bytes >= 0 AND committed_bytes >= 0 AND committed_bytes <= total_bytes'),
    check('migration_task_revision', 'revision > 0'),
    check('migration_task_pause', "(state = 'paused' AND resume_state IN ('queued','copying','verifying') AND pause_reason IN ('manual','traffic')) OR (state <> 'paused' AND resume_state = '' AND pause_reason = '')"),
    check('migration_task_state', "state IN ('admitting','prepared','admission_aborting','queued','copying','verifying','committing','cleanup_pending','paused','cancelling','cancelled','failed','completed')"))
Index('idx_migration_task_state', tasks.c.state, tasks.c.updated_at)
Index('idx_migration_task_source', tasks.c.source_id, tasks.c.state)
Index('idx_migration_task_target', tasks.c.target_id, tasks.c.state)
Index('idx_migration_catalog_commit',tasks.c.committed_bytes)

units = Table('migration_units', metadata,
    ident('task_id', primary=True), ident('unit_id', 64, primary=True), ident('resource_kind', 16),
    ident('parent_path', 1024), ident('source_version', 64), ident('state'),
    number('object_count'), number('total_bytes'), number('committed_at', default='0'),
    number('cleanup_after', default='0'),
    Column('publication',JSON,nullable=False,default=dict),
    Column('native_acknowledged',Integer,nullable=False,server_default='0'),
    check('migration_unit_native_ack','native_acknowledged IN (0,1)'),
    check('migration_unit_counts', 'object_count > 0 AND total_bytes >= 0'),
    check('migration_unit_kind', "resource_kind IN ('media','recording')"),
    check('migration_unit_state', "state IN ('pending','copying','verified','publishing','published','committed','cleanup_pending','cleaned','cancelled')"))

objects = Table('migration_objects', metadata,
    ident('task_id', primary=True), ident('logical_id', 64, primary=True), ident('unit_id', 64),
    ident('resource_kind', 16), ident('source_object', 64), ident('target_object', 64),
    ident('source_version', 64), ident('physical_path', 1024), number('expected_bytes'),
    Column('catalog_etag',String(128),nullable=False,default=''),
    Column('catalog_path_locator',String(64),nullable=False,default=''),
    Column('catalog_media_kind',String(16),nullable=False,default=''),
    Column('catalog_user_id',String(32),nullable=False,default=''),
    Column('catalog_encryption_json',Text,nullable=False,default=''),
    Column('catalog_encryption_file_id',String(32),nullable=False,default=''),
    Column('source_sha256', String(64), nullable=False, server_default=''),
    Column('target_sha256', String(64), nullable=False, server_default=''),
    number('verified_bytes', default='0'), ident('state'), number('updated_at'),
    check('migration_object_counts', 'expected_bytes >= 0 AND verified_bytes >= 0 AND verified_bytes <= expected_bytes'),
    check('migration_object_kind', "resource_kind IN ('media','recording')"),
    check('migration_object_state', "state IN ('pending','copying','verified','publishing','published','cleanup_pending','cleaned','cancelled')"))
Index('idx_migration_object_unit', objects.c.task_id, objects.c.unit_id, objects.c.state)

capacity = Table('migration_capacity', metadata,
    ident('task_id', primary=True), ident('member_id'), number('reserved_bytes'), number('updated_at'),
    check('migration_capacity_bytes', 'reserved_bytes >= 0'))
Index('idx_migration_capacity_member', capacity.c.member_id)

drains = Table('migration_drains', metadata,
    ident('member_id', primary=True), ident('owner_id', primary=True), ident('reason'), number('created_at'),
    check('migration_drain_reason', "reason IN ('migration','administrator')"))

scopes = Table('migration_scopes', metadata,
    ident('scope_id', 64, primary=True), ident('task_id'), ident('member_id'),
    ident('resource_kind', 16), ident('parent_path', 1024), number('created_at'),
    check('migration_scope_kind', "resource_kind IN ('media','recording')"))
Index('idx_migration_scope_task', scopes.c.task_id)

runners = Table('migration_runners', metadata,
    ident('task_id', primary=True), ident('runner_id'), number('epoch'), number('expires_at'),
    check('migration_runner_epoch', 'epoch > 0'))

storage_journal = Table('migration_storage_journal', metadata,
    ident('task_id', primary=True), ident('logical_id', 64, primary=True), ident('unit_id', 64),
    ident('master_id'), ident('source_id'), ident('target_id'), ident('resource_kind', 16),
    ident('object_id', 64), ident('physical_path', 1024), ident('source_version', 64),
    number('expected_bytes'), Column('sha256', String(64), nullable=False, server_default=''),
    Column('stage_identity',String(64),nullable=False,server_default=''),
    Column('cleanup_version',String(64),nullable=False,server_default=''),
    Column('publication',JSON,nullable=False,default=dict),
    ident('state'), number('updated_at'),
    Column('publication_phase',Integer,nullable=False,server_default='0'),
    check('migration_publication_phase','publication_phase >= 0 AND publication_phase <= 3'),
    check('migration_storage_members', 'source_id <> target_id AND expected_bytes >= 0'),
    check('migration_storage_kind', "resource_kind IN ('media','recording')"),
    check('migration_storage_state', "state IN ('receiving','verified','publishing','published','deleting','deleted','aborting','aborted')"))

read_leases = Table('migration_read_leases', metadata,
    ident('lease_id', primary=True), ident('member_id'), ident('logical_id', 64),
    ident('object_id', 64), ident('source_version', 64), number('expires_at'),
    Column('task_id',String(32),nullable=False,server_default=''),
    Column('purpose',String(16),nullable=False,server_default='playback'),
    check('migration_reader_purpose',"(purpose = 'migration' AND length(task_id) = 32) OR (purpose = 'playback' AND task_id = '')"))
Index('idx_migration_read_expiry', read_leases.c.member_id, read_leases.c.expires_at)
Index('idx_migration_read_task',read_leases.c.task_id,read_leases.c.purpose,read_leases.c.expires_at)

peer_bindings = Table('migration_peer_bindings', metadata,
    ident('task_id', primary=True), ident('logical_id', 64, primary=True),
    ident('unit_id',64),ident('source_path',1024),
    ident('binding_sha256', 64), Column('binding_json', Text, nullable=False))
Index('idx_migration_binding_unit',peer_bindings.c.task_id,peer_bindings.c.unit_id,peer_bindings.c.logical_id)
Index('idx_migration_binding_path',peer_bindings.c.task_id,peer_bindings.c.unit_id,peer_bindings.c.source_path,
      mysql_length={'source_path':191})
Index('idx_migration_storage_path',storage_journal.c.task_id,storage_journal.c.unit_id,storage_journal.c.physical_path,
      mysql_length={'physical_path':191})

# This generation is still unpublished. Keep native reservation authority
# separate from a Master task and from per-object transfer capabilities.
peer_reservations = Table('migration_peer_reservations', metadata,
    ident('task_id', primary=True), ident('master_id'), ident('member_id'), ident('relationship_id'),
    ident('side',16), ident('state',16), ident('binding_sha256',64), Column('payload',JSON,nullable=False),
    number('revision',default='1'), number('expected_units'), number('expected_objects'), number('expected_bytes'),
    number('accepted_units',default='0'), number('accepted_objects',default='0'), number('accepted_bytes',default='0'),
    Column('last_unit_id',String(64),nullable=False,server_default=''), ident('units_sha256',64),
    number('created_at'), number('updated_at'),
    check('migration_peer_side',"side IN ('source','target')"),
    check('migration_peer_state',"state IN ('preparing','prepared','active','aborting','aborted','released')"),
    check('migration_peer_revision','revision > 0'),
    check('migration_peer_counts','expected_units > 0 AND expected_objects >= expected_units AND expected_bytes > 0 '
        'AND accepted_units >= 0 AND accepted_units <= expected_units AND accepted_objects >= 0 '
        'AND accepted_objects <= expected_objects AND accepted_bytes >= 0 AND accepted_bytes <= expected_bytes'))
Index('idx_migration_peer_member',peer_reservations.c.member_id,peer_reservations.c.state)

plans = Table('migration_plan_headers', metadata,
    ident('plan_id', primary=True), ident('master_id'), ident('source_id'), ident('target_id'),
    ident('state', 16), Column('header_json', Text, nullable=False),
    number('object_count', default='0'), number('unit_count', default='0'), number('total_bytes', default='0'),
    Column('catalog_sha256', String(64), nullable=False, server_default=''),
    Column('proofs_sha256', String(64), nullable=False, server_default=''),
    Column('plan_sha256', String(64), nullable=False, server_default=''),
    number('created_at'), number('expires_at'),
    check('migration_plan_members', 'source_id <> target_id'),
    check('migration_plan_counts', 'object_count >= 0 AND unit_count >= 0 AND total_bytes >= 0'),
    check('migration_plan_state', "state IN ('observing','ready','blocked','admitted')"))
Index('idx_migration_plan_expiry', plans.c.state, plans.c.expires_at)

plan_bindings = Table('migration_plan_bindings', metadata,
    ident('plan_id', primary=True), ident('logical_id', 64, primary=True), ident('unit_id', 64),
    ident('resource_kind', 16), ident('source_object', 64), ident('object_sha256', 64),
    Column('object_json', Text, nullable=False), number('expected_bytes'),
    Column('source_sha256', String(64), nullable=False, server_default=''),
    Column('native_metadata_sha256', String(64), nullable=False, server_default=''),
    Column('native_seen', Integer, nullable=False, server_default='0'),
    UniqueConstraint('plan_id', 'resource_kind', 'source_object', name='uq_migration_plan_source'),
    check('migration_binding_bytes', 'expected_bytes > 0'),
    check('migration_binding_seen', 'native_seen IN (0,1)'),
    check('migration_binding_kind', "resource_kind IN ('media','recording')"))
Index('idx_migration_plan_unit',plan_bindings.c.plan_id,plan_bindings.c.unit_id,plan_bindings.c.logical_id)

plan_paths = Table('migration_plan_paths', metadata,
    ident('plan_id', primary=True), ident('resource_kind', 16, primary=True),
    ident('path_locator', 64, primary=True), ident('entry_kind', 16, primary=True),
    check('migration_path_kind', "resource_kind IN ('media','recording')"),
    check('migration_path_entry', "entry_kind IN ('name','parent','ancestor','admission')"))

for _table in metadata.tables.values():
    _table.dialect_options['mysql'].update(engine='InnoDB', charset='utf8mb4', collate='utf8mb4_bin')


def contract() -> str:
    records = []
    for dialect in (sqlite.dialect(), mysql.dialect()):
        for table in metadata.sorted_tables:
            constraints = [dict(name=constraint.name, kind=type(constraint).__name__,
                                columns=[column.name for column in constraint.columns],
                                expression=str(getattr(constraint, 'sqltext', '')))
                           for constraint in table.constraints]
            records.append(dict(dialect=dialect.name, table=table.name,
                columns=[str(CreateColumn(column).compile(dialect=dialect)) for column in table.columns],
                constraints=sorted(constraints, key=lambda value: json.dumps(value, sort_keys=True)),
                indexes=[str(CreateIndex(index).compile(dialect=dialect))
                         for index in sorted(table.indexes, key=lambda item: item.name)],
                mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collation='utf8mb4_bin'))
    return json.dumps(records, sort_keys=True, separators=(',', ':'))


async def validate(conn):
    from sqlalchemy import inspect
    def inspect_tables(sync):
        inspector = inspect(sync)
        for table in metadata.sorted_tables:
            actual = inspector.get_columns(table.name)
            if ({column['name'] for column in actual} != set(table.columns.keys()) or
                    any(column['nullable'] for column in actual)):
                raise RuntimeError(f'Migration journal columns invalid: {table.name}')
            for column in actual:
                expected = table.c[column['name']]
                if (column['type']._type_affinity is not expected.type._type_affinity or
                        getattr(column['type'], 'length', None) != getattr(expected.type, 'length', None) or
                        isinstance(column['type'], BigInteger) != isinstance(expected.type, BigInteger)):
                    raise RuntimeError(f'Migration journal column type invalid: {table.name}.{column["name"]}')
            primary = inspector.get_pk_constraint(table.name)['constrained_columns']
            if primary != [column.name for column in table.primary_key]:
                raise RuntimeError(f'Migration journal primary key invalid: {table.name}')
            indexes = {index['name'] for index in inspector.get_indexes(table.name)}
            if not {index.name for index in table.indexes} <= indexes:
                raise RuntimeError(f'Migration journal index missing: {table.name}')
            checks = {constraint['name'] for constraint in inspector.get_check_constraints(table.name)}
            expected = {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
            if not expected <= checks:
                raise RuntimeError(f'Migration journal check missing: {table.name}')
            uniques = {tuple(constraint['column_names']) for constraint in inspector.get_unique_constraints(table.name)}
            expected_uniques = {tuple(column.name for column in constraint.columns)
                                for constraint in table.constraints if isinstance(constraint, UniqueConstraint)}
            if not expected_uniques <= uniques:
                raise RuntimeError(f'Migration journal uniqueness missing: {table.name}')
    await conn.run_sync(inspect_tables)


async def create(conn):
    from app.core.schema_migrations import execute_ddl
    for table in metadata.sorted_tables:
        if conn.dialect.name == 'sqlite':
            await conn.run_sync(table.create, checkfirst=True)
        else:
            await execute_ddl(conn, str(CreateTable(table, if_not_exists=True).compile(dialect=mysql.dialect())))
        # MySQL can stop after CREATE TABLE but before CREATE INDEX. Retry each
        # index explicitly; create_all(checkfirst=True) alone cannot repair it.
        for index in sorted(table.indexes, key=lambda item: item.name):
            await conn.run_sync(index.create, checkfirst=True)
            if conn.dialect.name != 'sqlite':
                await conn.commit()
    await validate(conn)
