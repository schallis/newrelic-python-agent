import psycopg2
import pytest

from testing_support.fixtures import (validate_transaction_metrics,
    validate_database_trace_inputs, override_application_settings)
from utils import DB_MULTIPLE_SETTINGS, PSYCOPG2_VERSION, instance_hostname

from newrelic.agent import background_task


# Settings

_enable_instance_settings = {
    'datastore_tracer.instance_reporting.enabled': True,
}
_disable_instance_settings = {
    'datastore_tracer.instance_reporting.enabled': False,
}


# Metrics

_base_scoped_metrics = [
        ('Datastore/statement/Postgres/pg_settings/select', 1),
        ('Datastore/operation/Postgres/drop', 1),
        ('Datastore/operation/Postgres/create', 1),
        ('Datastore/operation/Postgres/commit', 2),
]

_base_rollup_metrics = [
        ('Datastore/all', 7),
        ('Datastore/allOther', 7),
        ('Datastore/Postgres/all', 7),
        ('Datastore/Postgres/allOther', 7),
        ('Datastore/statement/Postgres/pg_settings/select', 1),
        ('Datastore/operation/Postgres/drop', 1),
        ('Datastore/operation/Postgres/create', 1),
        ('Datastore/operation/Postgres/commit', 2),
]

if PSYCOPG2_VERSION > (2, 4):
    _base_scoped_metrics.append(
            ('Function/psycopg2:connect', 2))
else:
    _base_scoped_metrics.append(
            ('Function/psycopg2._psycopg:connect', 2))

_enable_scoped_metrics = list(_base_scoped_metrics)
_enable_rollup_metrics = list(_base_rollup_metrics)

_disable_scoped_metrics = list(_base_scoped_metrics)
_disable_rollup_metrics = list(_base_rollup_metrics)

if len(DB_MULTIPLE_SETTINGS) > 1:
    _postgresql_1 = DB_MULTIPLE_SETTINGS[0]
    _host_1 = instance_hostname(_postgresql_1['host'])
    _port_1 = _postgresql_1['port']

    _postgresql_2 = DB_MULTIPLE_SETTINGS[1]
    _host_2 = instance_hostname(_postgresql_2['host'])
    _port_2 = _postgresql_2['port']

    _instance_metrics = [
            ('Datastore/instance/Postgres/%s/%s' % (_host_1, _port_1), 2),
            ('Datastore/instance/Postgres/%s/%s' % (_host_2, _port_2), 3),
    ]

    _enable_scoped_metrics.extend(_instance_metrics)
    _enable_rollup_metrics.extend(_instance_metrics)


# Query

def _exercise_db():

    postgresql1 = DB_MULTIPLE_SETTINGS[0]
    postgresql2 = DB_MULTIPLE_SETTINGS[1]

    connection = psycopg2.connect(
            database=postgresql1['name'], user=postgresql1['user'],
            password=postgresql1['password'], host=postgresql1['host'],
            port=postgresql1['port'])
    try:
        cursor = connection.cursor()
        cursor.execute("""SELECT setting from pg_settings where name=%s""",
                ('server_version',))
        connection.commit()
    finally:
        connection.close()

    connection = psycopg2.connect(
            database=postgresql2['name'], user=postgresql2['user'],
            password=postgresql2['password'], host=postgresql2['host'],
            port=postgresql2['port'])
    try:
        cursor = connection.cursor()
        cursor.execute("""drop table if exists datastore_psycopg2""")
        cursor.execute("""create table datastore_psycopg2 """
                """(a integer, b real, c text)""")
        connection.commit()
    finally:
        connection.close()


# Tests

@pytest.mark.skipif(len(DB_MULTIPLE_SETTINGS) < 2,
        reason='Test environment not configured with multiple databases.')
@override_application_settings(_enable_instance_settings)
@validate_transaction_metrics(
        'test_multiple_dbs:test_multiple_databases_enable_instance',
        scoped_metrics=_enable_scoped_metrics,
        rollup_metrics=_enable_rollup_metrics,
        background_task=True)
@validate_database_trace_inputs(sql_parameters_type=tuple)
@background_task()
def test_multiple_databases_enable_instance():
    _exercise_db()

@pytest.mark.skipif(len(DB_MULTIPLE_SETTINGS) < 2,
        reason='Test environment not configured with multiple databases.')
@override_application_settings(_disable_instance_settings)
@validate_transaction_metrics(
        'test_multiple_dbs:test_multiple_databases_disable_instance',
        scoped_metrics=_disable_scoped_metrics,
        rollup_metrics=_disable_scoped_metrics,
        background_task=True)
@validate_database_trace_inputs(sql_parameters_type=tuple)
@background_task()
def test_multiple_databases_disable_instance():
    _exercise_db()
