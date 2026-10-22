"""The published-object faults on disposable MySQL, without duplicate suites."""
import unittest

from tests import s3_uploads_mysql_native as mysql_fixture, test_s3_objects as objects


class MySQLS3Objects(objects.S3ObjectsTests):
    fixture_type = mysql_fixture.MySQLS3Uploads


if __name__ == '__main__':
    unittest.main(verbosity=2)
