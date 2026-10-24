"""Generation 7: preserve the journal while extending safe pause boundaries."""
import re

from sqlalchemy import CheckConstraint,MetaData,inspect,text
from sqlalchemy.schema import CreateTable

from app.services import resource_migration_schema as s

RESUMABLE=frozenset({'queued','copying','verifying','committing','cleanup_pending'})
NAME='chk_migration_task_pause'
CHECK=("(state = 'paused' AND resume_state IN ('queued','copying','verifying','committing','cleanup_pending') "
    "AND pause_reason IN ('manual','traffic')) OR (state <> 'paused' AND resume_state = '' AND pause_reason = '')")
OLD_CHECK=str(next(value.sqltext for value in s.tasks.constraints if isinstance(value,CheckConstraint) and value.name==NAME))
TOKEN=re.compile(r"\s*(<>|!=|=|[(),]|'(?:[^']|'')*'|`[a-zA-Z_][a-zA-Z_0-9]*`|[a-zA-Z_][a-zA-Z_0-9]*)")


def canonical(expression):
    """Parse only this small check grammar, retaining Boolean precedence."""
    expression=re.sub(r"\b_utf8mb4(?=')",'',expression,flags=re.I)
    tokens=[];position=0
    while position<len(expression):
        match=TOKEN.match(expression,position)
        if match is None:
            if expression[position:].strip():raise RuntimeError('Unknown migration pause check syntax')
            break
        value=match.group(1)
        tokens.append(value if value.startswith("'") else value.strip('`').lower())
        position=match.end()
    offset=0
    def pop():
        nonlocal offset
        if offset>=len(tokens):raise RuntimeError('Incomplete migration pause check')
        value=tokens[offset];offset+=1;return value
    def expect(value):
        if pop()!=value:raise RuntimeError('Unknown migration pause check structure')
    def literal():
        value=pop()
        if not value.startswith("'"):raise RuntimeError('Invalid migration pause check literal')
        return value[1:-1].replace("''", "'")
    def group(operator,values):
        flattened=[]
        for value in values:
            flattened.extend(value[1] if value[0]==operator else [value])
        return (operator,tuple(sorted(flattened,key=repr))) if len(flattened)>1 else flattened[0]
    def atom():
        if offset<len(tokens) and tokens[offset]=='(':
            pop();value=expression_or();expect(')');return value
        name=pop()
        if name not in {'state','resume_state','pause_reason'}:raise RuntimeError('Unknown migration pause check column')
        operation=pop()
        if operation in {'=','<>','!='}:return ('<>' if operation=='!=' else operation,name,literal())
        if operation!='in':raise RuntimeError('Unknown migration pause check operator')
        expect('(');values=[literal()]
        while offset<len(tokens) and tokens[offset]==',':pop();values.append(literal())
        expect(')');return ('in',name,tuple(sorted(set(values))))
    def expression_and():
        values=[atom()]
        while offset<len(tokens) and tokens[offset]=='and':pop();values.append(atom())
        return group('and',values)
    def expression_or():
        values=[expression_and()]
        while offset<len(tokens) and tokens[offset]=='or':pop();values.append(expression_and())
        return group('or',values)
    result=expression_or()
    if offset!=len(tokens):raise RuntimeError('Unexpected migration pause check tokens')
    return result


async def current(conn):
    values=await conn.run_sync(lambda sync:inspect(sync).get_check_constraints('migration_tasks'))
    matches=[value['sqltext'] for value in values if value['name']==NAME]
    if len(matches)!=1:raise RuntimeError('Migration pause check missing or ambiguous')
    return canonical(matches[0])


async def validate(conn):
    if await current(conn)!=canonical(CHECK):raise RuntimeError('Migration pause check is not generation 7')


def _sql_tokens(expression):
    # Preserve string literals and grouping. Whitespace and keyword casing
    # cannot conceal an unknown constraint during the SQLite table rebuild.
    return tuple(value if value.startswith("'") else value.strip('"`').lower()
        for value in re.findall(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|[a-zA-Z_0-9]+|<>|!=|\S",expression))


async def _sqlite_preservable(conn):
    def known_shape(sync):
        inspector=inspect(sync)
        checks={value['name']:_sql_tokens(value['sqltext']) for value in inspector.get_check_constraints('migration_tasks')}
        expected={value.name:_sql_tokens(str(value.sqltext)) for value in s.tasks.constraints if isinstance(value,CheckConstraint)}
        indexes={value['name']:(tuple(value['column_names']),bool(value['unique']),bool(value.get('dialect_options')))
            for value in inspector.get_indexes('migration_tasks')}
        expected_indexes={value.name:(tuple(column.name for column in value.columns),bool(value.unique),False)
            for value in s.tasks.indexes}
        defaults={value['name']:_sql_tokens(value['default'] or '') for value in inspector.get_columns('migration_tasks')}
        def default_sql(column):
            if column.server_default is None:return ''
            value=column.server_default.arg
            return "'"+value.replace("'","''")+"'" if isinstance(value,str) else str(value)
        expected_defaults={value.name:_sql_tokens(default_sql(value)) for value in s.tasks.c}
        if (checks!=expected or indexes!=expected_indexes or defaults!=expected_defaults or
                inspector.get_unique_constraints('migration_tasks') or inspector.get_foreign_keys('migration_tasks')):
            raise RuntimeError('Unknown SQLite migration task structure; preserving existing journal')
    await conn.run_sync(known_shape)
    if await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='migration_tasks'")):
        raise RuntimeError('Unknown SQLite migration task trigger; preserving existing journal')
    names=(await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars().all()
    for name in names:
        if await conn.scalar(text('SELECT COUNT(*) FROM pragma_foreign_key_list(:name) WHERE "table" = :target'),
                {'name':name,'target':'migration_tasks'}):
            raise RuntimeError('Unknown incoming migration task reference; preserving existing journal')


async def apply(conn):
    from app.core.schema_migrations import execute_ddl,table_exists
    actual=await current(conn)
    if actual==canonical(CHECK):return
    if actual!=canonical(OLD_CHECK):raise RuntimeError('Unknown migration pause check; preserving existing journal')
    await s.validate(conn)
    if conn.dialect.name=='mysql':
        await execute_ddl(conn,f'ALTER TABLE migration_tasks DROP CHECK {NAME}, ADD CONSTRAINT {NAME} CHECK ({CHECK})')
    elif conn.dialect.name=='sqlite':
        await _sqlite_preservable(conn)
        temporary='migration_tasks_pause_v7'
        if await table_exists(conn,temporary):raise RuntimeError('Unexpected pause migration table; preserving both journals')
        table=s.tasks.to_metadata(MetaData(),name=temporary)
        for constraint in list(table.constraints):
            if isinstance(constraint,CheckConstraint) and constraint.name==NAME:table.constraints.remove(constraint)
        table.append_constraint(CheckConstraint(CHECK,name=NAME))
        await execute_ddl(conn,str(CreateTable(table).compile(dialect=conn.dialect)))
        columns=','.join('"'+name+'"' for name in s.tasks.c.keys())
        await conn.execute(text(f'INSERT INTO {temporary} ({columns}) SELECT {columns} FROM migration_tasks'))
        for left,right in (('migration_tasks',temporary),(temporary,'migration_tasks')):
            if await conn.scalar(text(f'SELECT EXISTS(SELECT {columns} FROM {left} EXCEPT SELECT {columns} FROM {right})')):
                raise RuntimeError('Pause migration row comparison failed; preserving existing journal')
        await execute_ddl(conn,'DROP TABLE migration_tasks')
        await execute_ddl(conn,f'ALTER TABLE {temporary} RENAME TO migration_tasks')
        for index in sorted(s.tasks.indexes,key=lambda value:value.name):await conn.run_sync(index.create)
    else:raise RuntimeError('Unsupported database for migration pause upgrade')
    await validate(conn)
