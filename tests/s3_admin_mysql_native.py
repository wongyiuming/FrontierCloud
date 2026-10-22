"""Actual MySQL behind the same HTTP and credential-redaction cases."""
import unittest

from tests import s3_uploads_mysql_native as mysql_fixture, test_s3_admin as admin


class MySQLS3Admin(admin.S3AdminTests):
    fixture_type = mysql_fixture.MySQLS3Uploads


if __name__ == '__main__':
    unittest.main(verbosity=2)
