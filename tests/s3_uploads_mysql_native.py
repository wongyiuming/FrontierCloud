"""Run the same recovery/concurrency faults against an actual disposable MySQL."""
import os
import unittest

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db
from tests import test_s3_uploads as fixtures


class MySQLS3Uploads(fixtures.S3UploadTests):
    async def open_database(self):
        url = make_url(os.environ['FC_DB_MYSQL_URL'])
        if os.environ.get('FC_DB_MYSQL_RUNTIME') != '1' or url.host != 'mysql' or url.database != 'frontiercloud_db_test':
            raise RuntimeError('Refusing S3 upload tests outside disposable mysql/frontiercloud_db_test')
        database = create_async_engine(url,pool_size=3,max_overflow=0)
        async with database.connect() as conn:
            for name in await db._table_names(conn):
                if not name.replace('_','').isalnum():
                    raise RuntimeError('unsafe fixture table')
                await conn.execute(text(f'DROP TABLE `{name}`'))
                await conn.commit()
        return database


if __name__ == '__main__':
    unittest.main(verbosity=2)
