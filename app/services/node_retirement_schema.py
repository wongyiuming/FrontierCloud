"""Generation 8: durable, identity-bound retirement fences and terminal receipts."""
import json
import re

from sqlalchemy import BigInteger,CheckConstraint,Column,Index,MetaData,String,Table,Text,UniqueConstraint,inspect,text
from sqlalchemy.dialects import mysql,sqlite
from sqlalchemy.schema import CreateColumn,CreateIndex,CreateTable

metadata=MetaData()
retirements=Table('node_retirements',metadata,
    Column('relationship_id',String(32),primary_key=True,nullable=False),
    Column('retirement_id',String(32),nullable=False),
    Column('node_id',String(32),nullable=False),Column('master_id',String(32),nullable=False),
    Column('member_id',String(32),nullable=False),Column('side',String(8),nullable=False),
    Column('phase',String(16),nullable=False),Column('binding_sha256',String(64),nullable=False),
    Column('authorization_sha256',String(64),nullable=False),
    Column('binding_json',Text,nullable=False),Column('roots_json',Text,nullable=False),Column('receipt_json',Text,nullable=False),
    Column('revision',BigInteger,nullable=False),Column('started_at',BigInteger,nullable=False),
    Column('completed_at',BigInteger,nullable=False),
    UniqueConstraint('retirement_id',name='uq_node_retirement_id'),
    CheckConstraint("side IN ('master','peer')",name='chk_node_retirement_side'),
    CheckConstraint("phase IN ('retiring','completed')",name='chk_node_retirement_phase'),
    CheckConstraint('revision > 0',name='chk_node_retirement_revision'),
    CheckConstraint("(phase = 'retiring' AND completed_at = 0) OR (phase = 'completed' AND completed_at >= started_at)",name='chk_node_retirement_completion'),
    CheckConstraint('started_at > 0',name='chk_node_retirement_start'))
Index('idx_node_retirement_phase',retirements.c.node_id,retirements.c.phase)
Index('idx_node_retirement_member',retirements.c.member_id,retirements.c.phase)
retirements.dialect_options['mysql'].update(engine='InnoDB',charset='utf8mb4',collate='utf8mb4_bin')

TOKEN=re.compile(r"\s*(>=|<=|<>|!=|>|<|=|[(),]|'(?:[^']|'')*'|`[a-zA-Z_][a-zA-Z_0-9]*`|[a-zA-Z_][a-zA-Z_0-9]*|[0-9]+)")


def canonical_check(expression):
    expression=re.sub(r"\b_utf8mb4(?=')",'',expression,flags=re.I)
    tokens=[];position=0
    while position<len(expression):
        match=TOKEN.match(expression,position)
        if match is None:
            if expression[position:].strip():raise RuntimeError('Unknown retirement check syntax')
            break
        value=match.group(1);tokens.append(value if value.startswith("'") else value.strip('`').lower());position=match.end()
    offset=0
    def pop():
        nonlocal offset
        if offset>=len(tokens):raise RuntimeError('Incomplete retirement check')
        value=tokens[offset];offset+=1;return value
    def expect(value):
        if pop()!=value:raise RuntimeError('Invalid retirement check grouping')
    def operand():
        value=pop()
        if value.startswith("'"):return ('literal',value[1:-1].replace("''", "'"))
        if value.isdecimal():return ('number',int(value))
        if value in retirements.c:return ('column',value)
        raise RuntimeError('Unknown retirement check operand')
    def group(operator,values):
        flat=[]
        for value in values:flat.extend(value[1] if value[0]==operator else [value])
        return (operator,tuple(sorted(flat,key=repr))) if len(flat)>1 else flat[0]
    def atom():
        if offset<len(tokens) and tokens[offset]=='(':
            pop();value=expression_or();expect(')');return value
        left=operand();operator=pop()
        if operator in {'=','<>','!=','>','>=','<','<='}:return ('<>' if operator=='!=' else operator,left,operand())
        if operator!='in':raise RuntimeError('Unknown retirement check operator')
        expect('(');values=[operand()]
        while offset<len(tokens) and tokens[offset]==',':pop();values.append(operand())
        expect(')');return ('in',left,tuple(sorted(set(values))))
    def expression_and():
        values=[atom()]
        while offset<len(tokens) and tokens[offset]=='and':pop();values.append(atom())
        return group('and',values)
    def expression_or():
        values=[expression_and()]
        while offset<len(tokens) and tokens[offset]=='or':pop();values.append(expression_and())
        return group('or',values)
    result=expression_or()
    if offset!=len(tokens):raise RuntimeError('Unexpected retirement check tokens')
    return result


def contract():
    records=[]
    for dialect in (sqlite.dialect(),mysql.dialect()):
        table=retirements
        records.append({'dialect':dialect.name,'table':table.name,
            'columns':[str(CreateColumn(column).compile(dialect=dialect)) for column in table.columns],
            'primary_key':[column.name for column in table.primary_key],
            'checks':sorted((value.name,str(value.sqltext)) for value in table.constraints if isinstance(value,CheckConstraint)),
            'uniques':sorted((value.name,[column.name for column in value.columns]) for value in table.constraints if isinstance(value,UniqueConstraint)),
            'indexes':[str(CreateIndex(value).compile(dialect=dialect)) for value in sorted(table.indexes,key=lambda value:value.name)],
            'mysql_engine':'InnoDB','mysql_charset':'utf8mb4','mysql_collation':'utf8mb4_bin'})
    return json.dumps(records,sort_keys=True,separators=(',',':'))


async def validate(conn,*,require_indexes=True):
    def observed(sync):
        inspector=inspect(sync);table=retirements;actual=inspector.get_columns(table.name)
        if {column['name'] for column in actual}!=set(table.c.keys()) or any(column['nullable'] for column in actual):
            raise RuntimeError('Retirement journal columns invalid')
        for column in actual:
            expected=table.c[column['name']]
            if (column['type']._type_affinity is not expected.type._type_affinity
                    or getattr(column['type'],'length',None)!=getattr(expected.type,'length',None)
                    or isinstance(column['type'],BigInteger)!=isinstance(expected.type,BigInteger)):
                raise RuntimeError('Retirement journal column type invalid: '+column['name'])
        if inspector.get_pk_constraint(table.name)['constrained_columns']!=['relationship_id'] or inspector.get_foreign_keys(table.name):
            raise RuntimeError('Retirement journal key structure invalid')
        check_rows=inspector.get_check_constraints(table.name)
        checks={value['name']:canonical_check(value['sqltext']) for value in check_rows}
        expected={value.name:canonical_check(str(value.sqltext)) for value in table.constraints if isinstance(value,CheckConstraint)}
        if checks!=expected or len(check_rows)!=len(expected):raise RuntimeError('Retirement journal checks invalid')
        indexes={value['name']:(tuple(value['column_names']),bool(value['unique'])) for value in inspector.get_indexes(table.name)}
        expected={value.name:(tuple(column.name for column in value.columns),bool(value.unique)) for value in table.indexes}
        if any(indexes[name]!=value for name,value in expected.items() if name in indexes) or require_indexes and not set(expected)<=indexes.keys():
            raise RuntimeError('Retirement journal indexes invalid')
        unique={tuple(value['column_names']) for value in inspector.get_unique_constraints(table.name)}
        if unique!={('retirement_id',)}:raise RuntimeError('Retirement journal uniqueness invalid')
        if sync.dialect.name=='mysql':
            options=inspector.get_table_options(table.name)
            if str(options.get('mysql_engine','')).lower()!='innodb' or options.get('mysql_collate')!='utf8mb4_bin':
                raise RuntimeError('Retirement journal requires transactional binary-collation storage')
            wrong_collation=sync.scalar(text("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name AND COLLATION_NAME IS NOT NULL AND COLLATION_NAME<>'utf8mb4_bin'"),{'name':table.name})
            if wrong_collation:raise RuntimeError('Retirement journal columns require binary collation')
            triggers=sync.scalar(text('SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:name'),{'name':table.name})
            unenforced=sync.scalar(text("SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name AND CONSTRAINT_TYPE='CHECK' AND ENFORCED<>'YES'"),{'name':table.name})
            if unenforced:raise RuntimeError('Retirement journal checks must be enforced')
        else:
            triggers=sync.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name=:name"),{'name':table.name})
        if triggers:raise RuntimeError('Unknown retirement journal trigger')
    await conn.run_sync(observed)


async def create(conn):
    from app.core.schema_migrations import execute_ddl
    if conn.dialect.name=='sqlite':await conn.run_sync(retirements.create,checkfirst=True)
    else:await execute_ddl(conn,str(CreateTable(retirements,if_not_exists=True).compile(dialect=mysql.dialect())))
    await validate(conn,require_indexes=False)
    for index in sorted(retirements.indexes,key=lambda value:value.name):
        await conn.run_sync(index.create,checkfirst=True)
        if conn.dialect.name!='sqlite':await conn.commit()
    await validate(conn)
