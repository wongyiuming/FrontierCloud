"""Store-local daily registration key admission for the shared account schema."""
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.services import karaoke_schema as schema


async def ensure_registration_day(connection, ip, day, now):
    values = dict(client_ip=ip, day_key=day, failure_count=0, success_count=0, updated_at=now)
    if getattr(getattr(connection, "dialect", None), "name", "mysql") == "sqlite":
        statement = sqlite_insert(schema.registration_daily).values(**values).on_conflict_do_nothing(
            index_elements=[schema.registration_daily.c.client_ip, schema.registration_daily.c.day_key])
    else:
        statement = mysql_insert(schema.registration_daily).values(**values).prefix_with("IGNORE")
    await connection.execute(statement)
