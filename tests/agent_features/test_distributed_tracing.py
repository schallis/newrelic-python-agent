import json
import pytest
import webtest

from newrelic.api.background_task import background_task
from newrelic.api.transaction import current_transaction
from newrelic.api.web_transaction import wsgi_application

from testing_support.fixtures import (override_application_settings,
        validate_attributes)

distributed_trace_intrinsics = ['parent.type', 'parent.app', 'parent.account',
        'parent.transportType', 'parent.transportDuration', 'grandparentId',
        'parentId', 'guid', 'nr.tripId', 'traceId', 'priority']


@wsgi_application()
def target_wsgi_application(environ, start_response):
    status = '200 OK'
    output = b'hello world'
    response_headers = [('Content-type', 'text/html; charset=utf-8'),
                        ('Content-Length', str(len(output)))]

    txn = current_transaction()

    # Make assertions on the WebTransaction object
    assert txn.is_distributed_trace
    assert txn.parent_type == 'App'
    assert txn.parent_app == '2827902'
    assert txn.parent_account == '1'
    assert txn.parent_transport_type == 'http'
    assert isinstance(txn.parent_transport_duration, float)
    assert txn._trace_id == 'd6b4ba0c3a712ca'
    assert txn.priority == 10.001
    assert txn.sampled
    assert txn.grandparent_id == '5e5733a911cfbc73'
    assert txn.parent_id == '7d3efb1b173fecfa'

    start_response(status, response_headers)
    return [output]


test_application = webtest.TestApp(target_wsgi_application)

_override_settings = {
    'trusted_account_ids': [1],
    'feature_flag': set(['distributed_tracing']),
}


@override_application_settings(_override_settings)
def test_distributed_tracing_web_transaction():
    payload = {
        'v': [0, 1],
        'd': {
            'ac': '1',
            'ap': '2827902',
            'id': '7d3efb1b173fecfa',
            'pa': '5e5733a911cfbc73',
            'pr': 10.001,
            'sa': True,
            'ti': 1518469636035,
            'tr': 'd6b4ba0c3a712ca',
            'ty': 'App',
        }
    }
    headers = {'X-NewRelic-Trace': json.dumps(payload)}

    response = test_application.get('/', headers=headers)
    assert 'X-NewRelic-App-Data' not in response.headers


@pytest.mark.parametrize('accept_payload', [True, False])
def test_distributed_trace_attributes(accept_payload):
    if accept_payload:
        _required_intrinsics = distributed_trace_intrinsics
        _forgone_intrinsics = []
    else:
        _required_intrinsics = []
        _forgone_intrinsics = distributed_trace_intrinsics

    @validate_attributes('intrinsic', _required_intrinsics,
            _forgone_intrinsics)
    @background_task(name='test_distributed_trace_attributes')
    def _test():
        txn = current_transaction()

        payload = {
            "v": [0, 1],
            "d": {
                "ty": "Mobile",
                "ac": "332029",
                "ap": "2827902",
                "pa": "5e5733a911cfbc73",
                "id": "7d3efb1b173fecfa",
                "tr": "d6b4ba0c3a712ca",
                "ti": 1518469636035
            }
        }
        if accept_payload:
            result = txn.accept_distributed_trace_payload(payload)
            assert result

    _test()
