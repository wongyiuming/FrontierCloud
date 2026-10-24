"""Generation 9: native metadata snapshots for cancellation reconciliation."""
import json
import re

from sqlalchemy import BigInteger,CheckConstraint,Column,Index,Integer,MetaData,String,Table,Text,inspect
from sqlalchemy.dialects import mysql,sqlite
from sqlalchemy.schema import CreateColumn,CreateIndex,CreateTable

metadata=MetaData()
snapshots=Table('migration_cancel_census',metadata,
    Column('task_id',String(32),primary_key=True,nullable=False),
    Column('logical_id',String(64),primary_key=True,nullable=False),
    Column('census_id',String(32),nullable=False),Column('resource_kind',String(16),nullable=False),
    Column('object_id',String(64),nullable=False),Column('physical_path',String(1024),nullable=False),
    Column('path_locator',String(64),nullable=False),Column('expected_bytes',BigInteger,nullable=False),
    Column('native_json',Text,nullable=False),Column('catalog_json',Text,nullable=False),
    Column('seen',Integer,nullable=False),
    CheckConstraint("resource_kind IN ('media','recording')",name='chk_cancel_census_kind'),
    CheckConstraint('expected_bytes > 0',name='chk_cancel_census_bytes'),
    CheckConstraint('seen IN (0,1)',name='chk_cancel_census_seen'))
Index('uq_cancel_census_object',snapshots.c.task_id,snapshots.c.resource_kind,snapshots.c.object_id,unique=True)
Index('uq_cancel_census_path',snapshots.c.task_id,snapshots.c.resource_kind,snapshots.c.path_locator,unique=True)
Index('idx_cancel_census_session',snapshots.c.task_id,snapshots.c.census_id,snapshots.c.seen)
snapshots.dialect_options['mysql'].update(engine='InnoDB',charset='utf8mb4',collate='utf8mb4_bin')


def contract():
    return json.dumps([dict(dialect=dialect.name,table=snapshots.name,
        columns=[str(CreateColumn(column).compile(dialect=dialect)) for column in snapshots.columns],
        primary=[column.name for column in snapshots.primary_key],
        checks=sorted((item.name,str(item.sqltext)) for item in snapshots.constraints if isinstance(item,CheckConstraint)),
        indexes=[str(CreateIndex(item).compile(dialect=dialect)) for item in sorted(snapshots.indexes,key=lambda item:item.name)])
        for dialect in (sqlite.dialect(),mysql.dialect())],sort_keys=True,separators=(',',':'))


def normalized_check(value):
    value=re.sub(r"\b_utf8mb4(?=')",'',value,flags=re.I)
    return re.sub(r'[\s`()]+','',value).lower()


async def validate(conn):
    def verify(sync):
        inspector=inspect(sync);columns=inspector.get_columns(snapshots.name)
        if {item['name'] for item in columns}!=set(snapshots.c.keys()):raise RuntimeError('Invalid cancellation census columns')
        for item in columns:
            expected=snapshots.c[item['name']]
            if (item['nullable'] or item['type']._type_affinity is not expected.type._type_affinity or
                    getattr(item['type'],'length',None)!=getattr(expected.type,'length',None) or
                    isinstance(item['type'],BigInteger)!=isinstance(expected.type,BigInteger)):
                raise RuntimeError('Invalid cancellation census column type')
        if inspector.get_pk_constraint(snapshots.name)['constrained_columns']!=['task_id','logical_id']:
            raise RuntimeError('Invalid cancellation census primary key')
        actual={item['name']:(item['column_names'],bool(item['unique'])) for item in inspector.get_indexes(snapshots.name)}
        expected={item.name:([column.name for column in item.columns],item.unique) for item in snapshots.indexes}
        if actual!=expected:raise RuntimeError('Invalid cancellation census indexes')
        actual={item['name']:normalized_check(item['sqltext']) for item in inspector.get_check_constraints(snapshots.name)}
        expected={item.name:normalized_check(str(item.sqltext)) for item in snapshots.constraints if isinstance(item,CheckConstraint)}
        if actual!=expected:raise RuntimeError('Invalid cancellation census checks')
    await conn.run_sync(verify)
    from sqlalchemy import text
    if conn.dialect.name=='sqlite':
        trigger=await conn.scalar(text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='migration_cancel_census' LIMIT 1"))
    else:
        trigger=await conn.scalar(text("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='migration_cancel_census' LIMIT 1"))
    if trigger:raise RuntimeError('Unknown cancellation census trigger')


async def create(conn):
    from app.core.schema_migrations import execute_ddl
    if conn.dialect.name=='sqlite':await conn.run_sync(snapshots.create,checkfirst=True)
    else:await execute_ddl(conn,str(CreateTable(snapshots,if_not_exists=True).compile(dialect=mysql.dialect())))
    for index in sorted(snapshots.indexes,key=lambda item:item.name):
        await conn.run_sync(index.create,checkfirst=True)
        if conn.dialect.name!='sqlite':await conn.commit()
    await validate(conn)
