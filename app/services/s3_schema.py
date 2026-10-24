"""Generation-6 optional S3 configuration and recoverable object transactions.

No credentials, upload or reservation is synthesized from bucket listings.
Keep published generations immutable; changes need a later migration.
"""
import json

from sqlalchemy import BigInteger, CheckConstraint, Column, Index, Integer, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

metadata = MetaData()


def ident(name,length=32,*,primary=False,default=None):
    return Column(name,String(length),nullable=False,primary_key=primary,server_default=default)


def number(name,*,default=None):
    return Column(name,BigInteger,nullable=False,server_default=default)


def check(name,expression):
    return CheckConstraint(expression,name='chk_s3_'+name)


members = Table('s3_storage_members',metadata,
    ident('member_id',primary=True),ident('master_id'),ident('name',80),
    Column('location_json',Text,nullable=False),Column('credentials_sealed',Text,nullable=False),
    ident('namespace_sha256',64),Column('enabled',Integer,nullable=False,server_default='0'),
    number('quota_bytes'),number('used_bytes',default='0'),number('reserved_bytes',default='0'),
    number('catalog_revision',default='0'),
    number('revision',default='1'),ident('probe_state',16,default='unchecked'),
    ident('error_code',64,default=''),number('probed_at',default='0'),number('created_at'),number('updated_at'),
    UniqueConstraint('namespace_sha256',name='uq_s3_namespace'),
    check('member_enabled','enabled IN (0,1)'),
    check('member_capacity','quota_bytes > 0 AND used_bytes >= 0 AND reserved_bytes >= 0 AND used_bytes + reserved_bytes <= quota_bytes'),
    check('member_revision','revision > 0'),
    check('member_catalog_revision','catalog_revision >= 0'),
    check('member_probe',"probe_state IN ('unchecked','ready','error')"))
Index('idx_s3_member_master',members.c.master_id,members.c.member_id)

uploads = Table('s3_upload_sessions',metadata,
    ident('session_id',primary=True),ident('member_id'),ident('master_id'),ident('media_id',64),
    ident('object_id',64),ident('path_locator',64),Column('media_path',Text,nullable=False),
    Column('object_key',Text,nullable=False),ident('provider_upload_id',2048,default=''),
    ident('state',24),number('revision',default='1'),number('expected_bytes'),number('part_bytes'),
    number('part_count'),number('credential_revision'),number('grant_until',default='0'),
    number('expires_at'),ident('checksum',64,default=''),ident('etag',128,default=''),
    ident('version_id',1024,default=''),ident('error_code',64,default=''),
    number('created_at'),number('updated_at'),
    UniqueConstraint('member_id','object_id',name='uq_s3_upload_object'),
    check('upload_state',"state IN ('reserved','creating','uploading','completing','verified','published','aborting','aborted','uncertain')"),
    check('upload_sizes','expected_bytes > 0 AND part_bytes >= 5242880 AND part_bytes <= 5368709120 AND part_count BETWEEN 1 AND 10000'),
    check('upload_revision','revision > 0 AND credential_revision > 0'),
    check('upload_deadlines','grant_until >= 0 AND expires_at > created_at'))
Index('idx_s3_upload_recovery',uploads.c.state,uploads.c.expires_at)
Index('idx_s3_upload_path',uploads.c.path_locator,uploads.c.state)
Index('idx_s3_upload_member',uploads.c.member_id,uploads.c.state)

parts = Table('s3_upload_parts',metadata,
    ident('session_id',primary=True),Column('part_number',Integer,nullable=False,primary_key=True),
    number('size_bytes'),ident('checksum_sha256',44),ident('etag',128,default=''),
    number('grant_until'),number('updated_at'),
    check('part_number','part_number BETWEEN 1 AND 10000'),
    check('part_size','size_bytes BETWEEN 1 AND 5368709120'),check('part_grant','grant_until > 0'))

objects = Table('s3_object_bindings',metadata,
    ident('media_id',64,primary=True),ident('member_id'),ident('object_id',64),ident('session_id'),
    Column('object_key',Text,nullable=False),number('size_bytes'),ident('checksum',64),ident('etag',128),
    ident('version_id',1024),ident('state',16),number('revision',default='1'),
    Column('content_encrypted',Integer,nullable=False,server_default='0'),
    number('grant_until',default='0'),ident('error_code',64,default=''),number('created_at'),number('updated_at'),
    UniqueConstraint('member_id','object_id',name='uq_s3_bound_object'),
    UniqueConstraint('session_id',name='uq_s3_bound_session'),
    check('object_size','size_bytes > 0'),check('object_revision','revision > 0 AND grant_until >= 0'),
    check('object_encryption','content_encrypted IN (0,1)'),
    check('object_state',"state IN ('active','deleting','deleted')"))
Index('idx_s3_object_member',objects.c.member_id,objects.c.state)
Index('idx_s3_object_recovery',objects.c.state,objects.c.grant_until,objects.c.updated_at)

for _table in metadata.tables.values():
    _table.dialect_options['mysql'].update(engine='InnoDB',charset='utf8mb4',collate='utf8mb4_bin')


def contract():
    records = []
    for dialect in (sqlite.dialect(),mysql.dialect()):
        for table in metadata.sorted_tables:
            constraints = [dict(name=value.name,kind=type(value).__name__,
                columns=[column.name for column in value.columns],expression=str(getattr(value,'sqltext','')))
                for value in table.constraints]
            records.append(dict(dialect=dialect.name,table=table.name,
                columns=[str(CreateColumn(column).compile(dialect=dialect)) for column in table.columns],
                constraints=sorted(constraints,key=lambda value:json.dumps(value,sort_keys=True)),
                indexes=[str(CreateIndex(value).compile(dialect=dialect)) for value in sorted(table.indexes,key=lambda value:value.name)]))
    return json.dumps(records,sort_keys=True,separators=(',',':'))


async def validate(conn):
    from sqlalchemy import inspect
    def inspect_tables(sync):
        inspector = inspect(sync)
        for table in metadata.sorted_tables:
            actual = inspector.get_columns(table.name)
            if {column['name'] for column in actual} != set(table.columns.keys()) or any(column['nullable'] for column in actual):
                raise RuntimeError('S3 transaction columns invalid: '+table.name)
            for column in actual:
                expected = table.c[column['name']]
                if (column['type']._type_affinity is not expected.type._type_affinity or
                    getattr(column['type'],'length',None) != getattr(expected.type,'length',None) or
                    isinstance(column['type'],BigInteger) != isinstance(expected.type,BigInteger)):
                    raise RuntimeError('S3 transaction column type invalid: '+table.name+'.'+column['name'])
            if inspector.get_pk_constraint(table.name)['constrained_columns'] != [column.name for column in table.primary_key]:
                raise RuntimeError('S3 transaction primary key invalid: '+table.name)
            if not {index.name for index in table.indexes} <= {index['name'] for index in inspector.get_indexes(table.name)}:
                raise RuntimeError('S3 transaction index missing: '+table.name)
            if not {value.name for value in table.constraints if isinstance(value,CheckConstraint)} <= {
                value['name'] for value in inspector.get_check_constraints(table.name)}:
                raise RuntimeError('S3 transaction check missing: '+table.name)
            if not {tuple(column.name for column in value.columns) for value in table.constraints if isinstance(value,UniqueConstraint)} <= {
                tuple(value['column_names']) for value in inspector.get_unique_constraints(table.name)}:
                raise RuntimeError('S3 transaction uniqueness missing: '+table.name)
    await conn.run_sync(inspect_tables)


async def create(conn):
    from app.core.schema_migrations import execute_ddl
    for table in metadata.sorted_tables:
        if conn.dialect.name == 'sqlite':
            await conn.run_sync(table.create,checkfirst=True)
        else:
            await execute_ddl(conn,str(CreateTable(table,if_not_exists=True).compile(dialect=mysql.dialect())))
        for index in sorted(table.indexes,key=lambda value:value.name):
            await conn.run_sync(index.create,checkfirst=True)
            if conn.dialect.name != 'sqlite':
                await conn.commit()
    await validate(conn)
