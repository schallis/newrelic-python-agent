from __future__ import print_function
import os
import re
import sys
import time
import threading
import logging
import itertools
import random
import warnings
import weakref

from collections import deque, OrderedDict

import newrelic.packages.six as six

import newrelic.core.transaction_node
import newrelic.core.database_node
import newrelic.core.error_node

from newrelic.core.stats_engine import CustomMetrics, SampledDataSet
from newrelic.core.trace_cache import trace_cache
from newrelic.core.thread_utilization import utilization_tracker

from newrelic.core.attribute import (create_attributes,
        create_agent_attributes, create_user_attributes,
        process_user_attribute, MAX_NUM_USER_ATTRIBUTES)
from newrelic.core.attribute_filter import (DST_NONE, DST_ERROR_COLLECTOR,
        DST_TRANSACTION_TRACER)
from newrelic.core.config import DEFAULT_RESERVOIR_SIZE
from newrelic.core.custom_event import create_custom_event
from newrelic.core.stack_trace import exception_stack
from newrelic.common.encoding_utils import (generate_path_hash, obfuscate,
        deobfuscate, json_encode, json_decode, base64_decode,
        convert_to_cat_metadata_value, DistributedTracePayload, ensure_str)

from newrelic.api.settings import STRIP_EXCEPTION_MESSAGE
from newrelic.api.time_trace import TimeTrace

_logger = logging.getLogger(__name__)

DISTRIBUTED_TRACE_KEYS_REQUIRED = ('ty', 'ac', 'ap', 'tr', 'ti')
DISTRIBUTED_TRACE_TRANSPORT_TYPES = set((
    'HTTP', 'HTTPS', 'Kafka', 'JMS',
    'IronMQ', 'AMQP', 'Queue', 'Other'))
HEXDIGLC_RE = re.compile('^[0-9a-f]+$')
DELIMITER_FORMAT_RE = re.compile('[ \t]*,[ \t]*')
ACCEPTED_DISTRIBUTED_TRACE = 1
CREATED_DISTRIBUTED_TRACE = 2
PARENT_TYPE = {
    '0': 'App',
    '1': 'Browser',
    '2': 'Mobile',
}


class Sentinel(TimeTrace):
    def __init__(self, transaction):
        super(Sentinel, self).__init__(None)
        self.transaction = transaction

        # Set the thread id to the same as the transaction before
        # saving in the cache.
        self.thread_id = transaction.thread_id
        trace_cache().save_trace(self)

    def process_child(self, node, ignore_exclusive=False):
        if ignore_exclusive:
            self.children.append(node)
        else:
            return super(Sentinel, self).process_child(node)

    def drop_trace(self):
        trace_cache().drop_trace(self)

    @property
    def transaction(self):
        return self._transaction and self._transaction()

    @transaction.setter
    def transaction(self, value):
        if value:
            self._transaction = weakref.ref(value)

    @property
    def root(self):
        return self

    @root.setter
    def root(self, value):
        pass


class CachedPath(object):
    def __init__(self, transaction):
        self._name = None
        self.transaction = weakref.ref(transaction)

    def path(self):
        if self._name is not None:
            return self._name

        transaction = self.transaction()
        if transaction:
            return transaction.path

        return 'Unknown'


class Transaction(object):

    STATE_PENDING = 0
    STATE_RUNNING = 1
    STATE_STOPPED = 2

    def __init__(self, application, enabled=None):

        self._application = application

        self.thread_id = None

        self._transaction_id = id(self)
        self._transaction_lock = threading.Lock()

        self._dead = False

        self._state = self.STATE_PENDING
        self._settings = None

        self._name_priority = 0
        self._group = None
        self._name = None
        self._cached_path = CachedPath(self)
        self._loop_time = 0.0

        self._frameworks = set()

        self._frozen_path = None

        self.root_span = None

        self._request_uri = None
        self._port = None

        self.queue_start = 0.0

        self.start_time = 0.0
        self.end_time = 0.0
        self.last_byte_time = 0.0

        self.total_time = 0.0

        self.stopped = False

        self._trace_node_count = 0

        self._errors = []
        self._slow_sql = []
        self._custom_events = SampledDataSet(capacity=DEFAULT_RESERVOIR_SIZE)

        self._stack_trace_count = 0
        self._explain_plan_count = 0

        self._string_cache = {}

        self._custom_params = {}
        self._request_params = {}

        self._utilization_tracker = None

        self._thread_utilization_start = None
        self._thread_utilization_end = None
        self._thread_utilization_value = None

        self._cpu_user_time_start = None
        self._cpu_user_time_end = None
        self._cpu_user_time_value = 0.0

        self._read_length = None

        self._read_start = None
        self._read_end = None

        self._sent_start = None
        self._sent_end = None

        self._bytes_read = 0
        self._bytes_sent = 0

        self._calls_read = 0
        self._calls_readline = 0
        self._calls_readlines = 0

        self._calls_write = 0
        self._calls_yield = 0

        self._transaction_metrics = {}

        self._agent_attributes = {}

        self.background_task = False

        self.enabled = False
        self.autorum_disabled = False

        self.ignore_transaction = False
        self.suppress_apdex = False
        self.suppress_transaction_trace = False

        self.capture_params = None

        self.apdex = 0

        self.rum_token = None

        # 16-digit random hex. Padded with zeros in the front.
        self.guid = '%016x' % random.getrandbits(64)

        # This may be overridden by processing an inbound CAT header
        self.parent_type = None
        self.parent_span = None
        self.trusted_parent_span = None
        self.tracing_vendors = None
        self.parent_tx = None
        self.parent_app = None
        self.parent_account = None
        self.parent_transport_type = None
        self.parent_transport_duration = None
        self.tracestate = ''
        self._trace_id = None
        self._priority = None
        self._sampled = None

        self._distributed_trace_state = 0

        self.client_cross_process_id = None
        self.client_account_id = None
        self.client_application_id = None
        self.referring_transaction_guid = None
        self.record_tt = False
        self._trip_id = None
        self._referring_path_hash = None
        self._alternate_path_hashes = {}
        self.is_part_of_cat = False

        self.synthetics_resource_id = None
        self.synthetics_job_id = None
        self.synthetics_monitor_id = None
        self.synthetics_header = None

        self._custom_metrics = CustomMetrics()

        self._profile_samples = deque()
        self._profile_frames = {}
        self._profile_skip = 1
        self._profile_count = 0

        global_settings = application.global_settings

        if global_settings.enabled:
            if enabled or (enabled is None and application.enabled):
                self._settings = application.settings
                if not self._settings:
                    application.activate()

                    # We see again if the settings is now valid
                    # in case startup timeout had been specified
                    # and registration had been started and
                    # completed within the timeout.

                    self._settings = application.settings

                if self._settings:
                    self.enabled = True

    def __del__(self):
        self._dead = True
        if self._state == self.STATE_RUNNING:
            self.__exit__(None, None, None)

    def __enter__(self):

        assert(self._state == self.STATE_PENDING)

        # Bail out if the transaction is not enabled.

        if not self.enabled:
            return self

        # Record the start time for transaction.

        self.start_time = time.time()

        # Record initial CPU user time.

        self._cpu_user_time_start = os.times()[0]

        # Set the thread ID upon entering the transaction.
        # This is done here so that any asyncio tasks will
        # be active and the task ID will be used to
        # store traces into the trace cache.
        self.thread_id = trace_cache().current_thread_id()

        # Calculate initial thread utilisation factor.
        # For now we only do this if we know it is an
        # actual thread and not a greenlet.

        if (not hasattr(sys, '_current_frames') or
                self.thread_id in sys._current_frames()):
            thread_instance = threading.currentThread()
            self._utilization_tracker = utilization_tracker(
                    self.application.name)
            if self._utilization_tracker:
                self._utilization_tracker.enter_transaction(thread_instance)
                self._thread_utilization_start = \
                        self._utilization_tracker.utilization_count()

        # Create the root span which pushes itself
        # into the trace cache as the active trace.
        self.root_span = Sentinel(self)

        # Mark transaction as active and update state
        # used to validate correct usage of class.

        self._state = self.STATE_RUNNING

        return self

    def __exit__(self, exc, value, tb):

        # Bail out if the transaction is not enabled.

        if not self.enabled:
            return

        if self._transaction_id != id(self):
            return

        if not self._settings:
            return

        # Force the root span out of the cache if it's there
        # This also prevents saving of the root span in the future since the
        # transaction will be None
        root = self.root_span
        root.drop_trace()

        self._state = self.STATE_STOPPED

        # Record error if one was registered.

        if exc is not None and value is not None and tb is not None:
            self.record_exception(exc, value, tb)

        # Record the end time for transaction and then
        # calculate the duration.

        if not self.stopped:
            self.end_time = time.time()

        # Calculate transaction duration

        duration = self.end_time - self.start_time

        # Calculate response time. Calculation depends on whether
        # a web response was sent back.

        if self.last_byte_time == 0.0:
            response_time = duration
        else:
            response_time = self.last_byte_time - self.start_time

        # Calculate overall user time.

        if not self._cpu_user_time_end:
            self._cpu_user_time_end = os.times()[0]

        if duration and self._cpu_user_time_end:
            self._cpu_user_time_value = (self._cpu_user_time_end -
                    self._cpu_user_time_start)

        # Calculate thread utilisation factor. Note that even if
        # we are tracking thread utilization we skip calculation
        # if duration is zero. Under normal circumstances this
        # should not occur but may if the system clock is wound
        # backwards and duration was squashed to zero due to the
        # request appearing to finish before it started. It may
        # also occur if true response time came in under the
        # resolution of the clock being used, but that is highly
        # unlikely as the overhead of the agent itself should
        # always ensure that that is hard to achieve.

        if self._utilization_tracker:
            self._utilization_tracker.exit_transaction()
            if self._thread_utilization_start is not None and duration > 0.0:
                if not self._thread_utilization_end:
                    self._thread_utilization_end = (
                            self._utilization_tracker.utilization_count())
                self._thread_utilization_value = (
                        self._thread_utilization_end -
                        self._thread_utilization_start) / duration

        # Derive generated values from the raw data. The
        # dummy root node has exclusive time of children
        # as negative number. Add our own duration to get
        # our own exclusive time.

        children = root.children

        exclusive = duration + root.exclusive

        # Add transaction exclusive time to total exclusive time
        #
        self.total_time += exclusive

        # Construct final root node of transaction trace.
        # Freeze path in case not already done. This will
        # construct out path.

        self._freeze_path()

        # _sent_end should already be set by this point, but in case it
        # isn't, set it now before we record the custom metrics.

        if self._sent_start:
            if not self._sent_end:
                self._sent_end = time.time()

        if self.client_cross_process_id is not None:
            metric_name = 'ClientApplication/%s/all' % (
                    self.client_cross_process_id)
            self.record_custom_metric(metric_name, duration)

        # Record supportability metrics for api calls

        for key, value in six.iteritems(self._transaction_metrics):
            self.record_custom_metric(key, {'count': value})

        if self._frameworks:
            for framework, version in self._frameworks:
                self.record_custom_metric('Python/Framework/%s/%s' %
                    (framework, version), 1)

        if self._settings.distributed_tracing.enabled:
            # Sampled and priority need to be computed at the end of the
            # transaction when distributed tracing or span events are enabled.
            self._compute_sampled_and_priority()

        self._cached_path._name = self.path
        node = newrelic.core.transaction_node.TransactionNode(
                settings=self._settings,
                path=self.path,
                type=self.type,
                group=self.group_for_metric,
                base_name=self._name,
                name_for_metric=self.name_for_metric,
                port=self._port,
                request_uri=self._request_uri,
                queue_start=self.queue_start,
                start_time=self.start_time,
                end_time=self.end_time,
                last_byte_time=self.last_byte_time,
                total_time=self.total_time,
                response_time=response_time,
                duration=duration,
                exclusive=exclusive,
                children=tuple(children),
                errors=tuple(self._errors),
                slow_sql=tuple(self._slow_sql),
                custom_events=self._custom_events,
                apdex_t=self.apdex,
                suppress_apdex=self.suppress_apdex,
                custom_metrics=self._custom_metrics,
                guid=self.guid,
                cpu_time=self._cpu_user_time_value,
                suppress_transaction_trace=self.suppress_transaction_trace,
                client_cross_process_id=self.client_cross_process_id,
                referring_transaction_guid=self.referring_transaction_guid,
                record_tt=self.record_tt,
                synthetics_resource_id=self.synthetics_resource_id,
                synthetics_job_id=self.synthetics_job_id,
                synthetics_monitor_id=self.synthetics_monitor_id,
                synthetics_header=self.synthetics_header,
                is_part_of_cat=self.is_part_of_cat,
                trip_id=self.trip_id,
                path_hash=self.path_hash,
                referring_path_hash=self._referring_path_hash,
                alternate_path_hashes=self.alternate_path_hashes,
                trace_intrinsics=self.trace_intrinsics,
                distributed_trace_intrinsics=self.distributed_trace_intrinsics,
                agent_attributes=self.agent_attributes,
                user_attributes=self.user_attributes,
                priority=self.priority,
                sampled=self.sampled,
                parent_span=self.parent_span,
                parent_transport_duration=self.parent_transport_duration,
                parent_type=self.parent_type,
                parent_account=self.parent_account,
                parent_app=self.parent_app,
                parent_tx=self.parent_tx,
                parent_transport_type=self.parent_transport_type,
                root_span_guid=root.guid,
                trace_id=self.trace_id,
                loop_time=self._loop_time,
                trusted_parent_span=self.trusted_parent_span,
                tracing_vendors=self.tracing_vendors,
        )

        # Clear settings as we are all done and don't need it
        # anymore.

        self._settings = None
        self.enabled = False

        # Unless we are ignoring the transaction, record it. We
        # need to lock the profile samples and replace it with
        # an empty list just in case the thread profiler kicks
        # in just as we are trying to record the transaction.
        # If we don't, when processing the samples, addition of
        # new samples can cause an error.

        if not self.ignore_transaction:
            profile_samples = []

            if self._profile_samples:
                with self._transaction_lock:
                    profile_samples = self._profile_samples
                    self._profile_samples = deque()

            self._application.record_transaction(node,
                    (self.background_task, profile_samples))

    @property
    def sampled(self):
        return self._sampled

    @property
    def priority(self):
        return self._priority

    @property
    def state(self):
        return self._state

    @property
    def is_distributed_trace(self):
        return self._distributed_trace_state != 0

    @property
    def settings(self):
        return self._settings

    @property
    def application(self):
        return self._application

    @property
    def type(self):
        if self.background_task:
            transaction_type = 'OtherTransaction'
        else:
            transaction_type = 'WebTransaction'
        return transaction_type

    @property
    def name(self):
        return self._name

    @property
    def group(self):
        return self._group

    @property
    def name_for_metric(self):
        """Combine group and name for use as transaction name in metrics."""
        group = self.group_for_metric

        transaction_name = self._name

        if transaction_name is None:
            transaction_name = '<undefined>'

        # Stripping the leading slash on the request URL held by
        # transaction_name when type is 'Uri' is to keep compatibility
        # with PHP agent and also possibly other agents. Leading
        # slash it not deleted for other category groups as the
        # leading slash may be significant in that situation.

        if (group in ('Uri', 'NormalizedUri') and
                transaction_name.startswith('/')):
            name = '%s%s' % (group, transaction_name)
        else:
            name = '%s/%s' % (group, transaction_name)

        return name

    @property
    def group_for_metric(self):
        _group = self._group

        if _group is None:
            if self.background_task:
                _group = 'Python'
            else:
                _group = 'Uri'

        return _group

    @property
    def path(self):
        if self._frozen_path:
            return self._frozen_path

        return '%s/%s' % (self.type, self.name_for_metric)

    @property
    def profile_sample(self):
        return self._profile_samples

    @property
    def trip_id(self):
        return self._trip_id or self.guid

    @property
    def trace_id(self):
        trace_id = self._trace_id
        if trace_id:
            return trace_id

        if self._settings.distributed_tracing.format == 'w3c':
            # Prevent all zeros trace id (illegal)
            while not trace_id:
                trace_id = random.getrandbits(128)
            self._trace_id = '{:032x}'.format(trace_id)
            return self._trace_id

        return self.guid

    @property
    def alternate_path_hashes(self):
        """Return the alternate path hashes but not including the current path
        hash.

        """
        return sorted(set(self._alternate_path_hashes.values()) -
                set([self.path_hash]))

    @property
    def path_hash(self):
        """Path hash is a 32-bit digest of the string "appname;txn_name"
        XORed with the referring_path_hash. Since the txn_name can change
        during the course of a transaction, up to 10 path_hashes are stored
        in _alternate_path_hashes. Before generating the path hash, check the
        _alternate_path_hashes to determine if we've seen this identifier and
        return the value.

        """

        if not self.is_part_of_cat:
            return None

        identifier = '%s;%s' % (self.application.name, self.path)

        # Check if identifier is already part of the _alternate_path_hashes and
        # return the value if available.

        if self._alternate_path_hashes.get(identifier):
            return self._alternate_path_hashes[identifier]

        # If the referring_path_hash is unavailable then we use '0' as the
        # seed.

        try:
            seed = int((self._referring_path_hash or '0'), base=16)
        except Exception:
            seed = 0

        path_hash = generate_path_hash(identifier, seed)

        # Only store upto 10 alternate path hashes.

        if len(self._alternate_path_hashes) < 10:
            self._alternate_path_hashes[identifier] = path_hash

        return path_hash

    @property
    def attribute_filter(self):
        return self._settings.attribute_filter

    @property
    def read_duration(self):
        read_duration = 0
        if self._read_start and self._read_end:
            read_duration = self._read_end - self._read_start
        return read_duration

    @property
    def sent_duration(self):
        sent_duration = 0
        if self._sent_start and self._sent_end:
            sent_duration = self._sent_end - self._sent_start
        return sent_duration

    @property
    def queue_wait(self):
        queue_wait = 0
        if self.queue_start:
            queue_wait = self.start_time - self.queue_start
            if queue_wait < 0:
                queue_wait = 0
        return queue_wait

    @property
    def should_record_segment_params(self):
        # Only record parameters when it is safe to do so
        return (self.settings and
                not self.settings.high_security)

    @property
    def trace_intrinsics(self):
        """Intrinsic attributes for transaction traces and error traces"""
        i_attrs = {}

        if self.referring_transaction_guid:
            i_attrs['referring_transaction_guid'] = \
                    self.referring_transaction_guid
        if self.client_cross_process_id:
            i_attrs['client_cross_process_id'] = self.client_cross_process_id
        if self.trip_id:
            i_attrs['trip_id'] = self.trip_id
        if self.path_hash:
            i_attrs['path_hash'] = self.path_hash
        if self.synthetics_resource_id:
            i_attrs['synthetics_resource_id'] = self.synthetics_resource_id
        if self.synthetics_job_id:
            i_attrs['synthetics_job_id'] = self.synthetics_job_id
        if self.synthetics_monitor_id:
            i_attrs['synthetics_monitor_id'] = self.synthetics_monitor_id
        if self.total_time:
            i_attrs['totalTime'] = self.total_time
        if self._loop_time:
            i_attrs['eventLoopTime'] = self._loop_time

        # Add in special CPU time value for UI to display CPU burn.

        # XXX Disable cpu time value for CPU burn as was
        # previously reporting incorrect value and we need to
        # fix it, at least on Linux to report just the CPU time
        # for the executing thread.

        # if self._cpu_user_time_value:
        #     i_attrs['cpu_time'] = self._cpu_user_time_value

        i_attrs.update(self.distributed_trace_intrinsics)

        return i_attrs

    @property
    def distributed_trace_intrinsics(self):
        i_attrs = {}

        if not self._settings.distributed_tracing.enabled:
            return i_attrs

        i_attrs['guid'] = self.guid
        i_attrs['sampled'] = self.sampled
        i_attrs['priority'] = self.priority
        i_attrs['traceId'] = self.trace_id

        if not self._distributed_trace_state:
            return i_attrs

        if self.parent_type:
            i_attrs['parent.type'] = self.parent_type
        if self.parent_account:
            i_attrs['parent.account'] = self.parent_account
        if self.parent_app:
            i_attrs['parent.app'] = self.parent_app
        if self.parent_transport_type:
            i_attrs['parent.transportType'] = self.parent_transport_type
        if self.parent_transport_duration:
            i_attrs['parent.transportDuration'] = \
                    self.parent_transport_duration
        if self.trusted_parent_span:
            i_attrs['trustedParentId'] = self.trusted_parent_span
        if self.tracing_vendors:
            i_attrs['tracingVendors'] = self.tracing_vendors

        return i_attrs

    @property
    def request_parameters_attributes(self):
        # Request parameters are a special case of agent attributes, so
        # they must be added on to agent_attributes separately

        # There are 3 cases we need to handle:
        #
        # 1. LEGACY: capture_params = False
        #
        #    Don't add request parameters at all, which means they will not
        #    go through the AttributeFilter.
        #
        # 2. LEGACY: capture_params = True
        #
        #    Filter request parameters through the AttributeFilter, but
        #    set the destinations to `TRANSACTION_TRACER | ERROR_COLLECTOR`.
        #
        #    If the user does not add any additional attribute filtering
        #    rules, this will result in the same outcome as the old
        #    capture_params = True behavior. They will be added to transaction
        #    traces and error traces.
        #
        # 3. CURRENT: capture_params is None
        #
        #    Filter request parameters through the AttributeFilter, but set
        #    the destinations to NONE.
        #
        #    That means by default, request parameters won't get included in
        #    any destination. But, it will allow user added include/exclude
        #    attribute filtering rules to be applied to the request parameters.

        attributes_request = []

        if (self.capture_params is None) or self.capture_params:

            if self._request_params:

                r_attrs = {}

                for k, v in self._request_params.items():
                    new_key = 'request.parameters.%s' % k
                    new_val = ",".join(v)

                    final_key, final_val = process_user_attribute(new_key,
                            new_val)

                    if final_key:
                        r_attrs[final_key] = final_val

                if self.capture_params is None:
                    attributes_request = create_attributes(r_attrs,
                            DST_NONE, self.attribute_filter)
                elif self.capture_params:
                    attributes_request = create_attributes(r_attrs,
                            DST_ERROR_COLLECTOR | DST_TRANSACTION_TRACER,
                            self.attribute_filter)

        return attributes_request

    def _add_agent_attribute(self, key, value):
        self._agent_attributes[key] = value

    @property
    def agent_attributes(self):
        a_attrs = self._agent_attributes

        if self._settings.process_host.display_name:
            a_attrs['host.displayName'] = \
                    self._settings.process_host.display_name
        if self._thread_utilization_value:
            a_attrs['thread.concurrency'] = self._thread_utilization_value
        if self.queue_wait != 0:
            a_attrs['webfrontend.queue.seconds'] = self.queue_wait

        agent_attributes = create_agent_attributes(a_attrs,
                self.attribute_filter)

        # Include request parameters in agent attributes

        agent_attributes.extend(self.request_parameters_attributes)

        return agent_attributes

    @property
    def user_attributes(self):
        return create_user_attributes(self._custom_params,
                self.attribute_filter)

    def add_profile_sample(self, stack_trace):
        if self._state != self.STATE_RUNNING:
            return

        self._profile_count += 1

        if self._profile_count < self._profile_skip:
            return

        self._profile_count = 0

        with self._transaction_lock:
            new_stack_trace = tuple(self._profile_frames.setdefault(
                    frame, frame) for frame in stack_trace)
            self._profile_samples.append(new_stack_trace)

            agent_limits = self._application.global_settings.agent_limits
            profile_maximum = agent_limits.xray_profile_maximum

            if len(self._profile_samples) >= profile_maximum:
                self._profile_samples = deque(itertools.islice(
                        self._profile_samples, 0,
                        len(self._profile_samples), 2))
                self._profile_skip = 2 * self._profile_skip

    def _compute_sampled_and_priority(self):
        if self._priority is None:
            # truncate priority field to 5 digits past the decimal
            self._priority = float('%.5f' % random.random())

        if self._sampled is None:
            self._sampled = self._application.compute_sampled()
            if self._sampled:
                self._priority += 1

    def _freeze_path(self):
        if self._frozen_path is None:
            self._name_priority = None

            if self._group == 'Uri' and self._name != '/':
                # Apply URL normalization rules. We would only have raw
                # URLs where we were not specifically naming the web
                # transactions for a specific web framework to be a code
                # handler or otherwise.

                name, ignore = self._application.normalize_name(
                        self._name, 'url')

                if self._name != name:
                    self._group = 'NormalizedUri'
                    self._name = name

                self.ignore_transaction = self.ignore_transaction or ignore

            # Apply transaction rules on the full transaction name.

            path, ignore = self._application.normalize_name(
                    self.path, 'transaction')

            self.ignore_transaction = self.ignore_transaction or ignore

            # Apply segment whitelist rule to the segments on the full
            # transaction name. The path is frozen at this point and cannot be
            # further changed.

            self._frozen_path, ignore = self._application.normalize_name(
                    path, 'segment')

            self.ignore_transaction = self.ignore_transaction or ignore

            # Look up the apdex from the table of key transactions. If
            # current transaction is not a key transaction then use the
            # default apdex from settings. The path used at this point
            # is the frozen path.

            self.apdex = (self._settings.web_transactions_apdex.get(
                self.path) or self._settings.apdex_t)

    def _record_supportability(self, metric_name):
        m = self._transaction_metrics.get(metric_name, 0)
        self._transaction_metrics[metric_name] = m + 1

    def _create_distributed_trace_payload_with_guid(self, guid):
        payload = self.create_distributed_trace_payload()
        if guid and payload and 'id' in payload['d']:
            payload['d']['id'] = guid
        return payload

    def _create_distributed_trace_payload(self):
        if not self.enabled:
            return

        settings = self._settings
        account_id = settings.account_id
        trusted_account_key = settings.trusted_account_key
        application_id = settings.primary_application_id

        if not (account_id and
                application_id and
                trusted_account_key and
                settings.distributed_tracing.enabled):
            return

        try:
            self._compute_sampled_and_priority()
            data = dict(
                ty='App',
                ac=account_id,
                ap=application_id,
                tr=self.trace_id,
                sa=self.sampled,
                pr=self.priority,
                tx=self.guid,
                ti=int(time.time() * 1000.0),
            )

            if account_id != trusted_account_key:
                data['tk'] = trusted_account_key

            current_span = trace_cache().current_trace()
            if (settings.span_events.enabled and
                    settings.collect_span_events and
                    current_span and self.sampled):
                data['id'] = current_span.guid

            self._distributed_trace_state |= CREATED_DISTRIBUTED_TRACE

            self._record_supportability('Supportability/DistributedTrace/'
                    'CreatePayload/Success')
            return DistributedTracePayload(
                v=DistributedTracePayload.version,
                d=data,
            )
        except:
            self._record_supportability('Supportability/DistributedTrace/'
                    'CreatePayload/Exception')

    def create_distributed_trace_payload(self):
        warnings.warn((
            'The create_distributed_trace_payload API has been deprecated. '
            'Please use the insert_distributed_trace_headers API.'
        ), DeprecationWarning)
        return self._create_distributed_trace_payload()

    def _generate_tracestate_header(self):
        if not self.enabled or not self.settings.span_events.enabled:
            return self.tracestate

        settings = self._settings
        self._compute_sampled_and_priority()

        account_id = settings.account_id
        trusted_account_key = settings.trusted_account_key
        application_id = settings.primary_application_id

        current_span = trace_cache().current_trace()
        timestamp = str(int(time.time() * 1000.0))

        nr_payload = '-'.join((
            '0-0',
            account_id,
            application_id,
            current_span.guid,
            self.guid,
            '1' if self._sampled else '0',
            '%.5g' % self._priority,
            timestamp,
        ))

        nr_entry = '{}@nr={},'.format(
            trusted_account_key,
            nr_payload,
        )

        tracestate = nr_entry + self.tracestate
        return tracestate

    def _generate_traceparent_header(self):
        self._compute_sampled_and_priority()
        current_span = trace_cache().current_trace()
        format_str = '00-{}-{}-{:02x}'
        if self._settings.span_events.enabled and current_span:
            return format_str.format(
                self.trace_id,
                current_span.guid,
                int(self.sampled),
            )
        else:
            return format_str.format(
                self.trace_id,
                '{:016x}'.format(random.getrandbits(64)),
                int(self.sampled),
            )

    def insert_distributed_trace_headers(self, headers):
        if self.settings.distributed_tracing.format == 'w3c':
            try:
                traceparent = self._generate_traceparent_header()
                headers.append(("traceparent", traceparent))

                tracestate = self._generate_tracestate_header()
                if tracestate:
                    headers.append(("tracestate", tracestate))
                self._record_supportability('Supportability/TraceContext/'
                        'Create/Success')
            except:
                self._record_supportability('Supportability/TraceContext/'
                        'Create/Exception')
        else:
            payload = self._create_distributed_trace_payload()
            payload = payload and payload.http_safe()
            headers.append(('newrelic', payload))

    def _can_accept_distributed_trace_headers(self):
        if not self.enabled:
            return False

        settings = self._settings
        if not (settings.distributed_tracing.enabled and
                settings.trusted_account_key):
            return False

        if self._distributed_trace_state:
            if self._distributed_trace_state & ACCEPTED_DISTRIBUTED_TRACE:
                self._record_supportability('Supportability/DistributedTrace/'
                        'AcceptPayload/Ignored/Multiple')
            else:
                self._record_supportability('Supportability/DistributedTrace/'
                        'AcceptPayload/Ignored/CreateBeforeAccept')
            return False

        return True

    def _accept_distributed_trace_payload(
            self, payload, transport_type='HTTP'):
        if not payload:
            self._record_supportability('Supportability/DistributedTrace/'
                    'AcceptPayload/Ignored/Null')
            return False

        payload = DistributedTracePayload.decode(payload)
        if not payload:
            self._record_supportability('Supportability/DistributedTrace/'
                    'AcceptPayload/ParseException')
            return False

        try:
            version = payload.get('v')
            major_version = version and int(version[0])

            if major_version is None:
                self._record_supportability('Supportability/DistributedTrace/'
                        'AcceptPayload/ParseException')
                return False

            if major_version > DistributedTracePayload.version[0]:
                self._record_supportability('Supportability/DistributedTrace/'
                        'AcceptPayload/Ignored/MajorVersion')
                return False

            data = payload.get('d', {})
            if not all(k in data for k in DISTRIBUTED_TRACE_KEYS_REQUIRED):
                self._record_supportability('Supportability/DistributedTrace/'
                        'AcceptPayload/ParseException')
                return False

            # Must have either id or tx
            if not any(k in data for k in ('id', 'tx')):
                self._record_supportability('Supportability/DistributedTrace/'
                                            'AcceptPayload/ParseException')
                return False

            settings = self._settings
            account_id = data.get('ac')

            # If trust key doesn't exist in the payload, use account_id
            received_trust_key = data.get('tk', account_id)
            if settings.trusted_account_key != received_trust_key:
                self._record_supportability('Supportability/DistributedTrace/'
                        'AcceptPayload/Ignored/UntrustedAccount')
                if settings.debug.log_untrusted_distributed_trace_keys:
                    _logger.debug('Received untrusted key in distributed '
                            'trace payload. received_trust_key=%r',
                            received_trust_key)
                return False

            transport_start = data.get('ti') / 1000.0

            self.parent_type = data.get('ty')

            self.parent_span = data.get('id')
            self.parent_tx = data.get('tx')
            self.parent_app = data.get('ap')
            self.parent_account = account_id

            if transport_type not in DISTRIBUTED_TRACE_TRANSPORT_TYPES:
                transport_type = 'Unknown'

            self.parent_transport_type = transport_type

            # If starting in the future, transport duration should be set to 0
            now = time.time()
            if transport_start > now:
                self.parent_transport_duration = 0.0
            else:
                self.parent_transport_duration = now - transport_start

            self._trace_id = data.get('tr')

            if 'pr' in data:
                self._priority = data.get('pr')
                self._sampled = data.get('sa', self._sampled)

            self._distributed_trace_state = ACCEPTED_DISTRIBUTED_TRACE

            self._record_supportability('Supportability/DistributedTrace/'
                    'AcceptPayload/Success')
            return True

        except:
            self._record_supportability('Supportability/DistributedTrace/'
                    'AcceptPayload/Exception')
            return False

    def accept_distributed_trace_payload(self, *args, **kwargs):
        warnings.warn((
            'The accept_distributed_trace_payload API has been deprecated. '
            'Please use the accept_distributed_trace_headers API.'
        ), DeprecationWarning)
        if not self._can_accept_distributed_trace_headers():
            return False
        return self._accept_distributed_trace_payload(*args, **kwargs)

    def _parse_traceparent_header(self, traceparent, transport_type):
        version_payload = traceparent.split('-', 1)

        # If there's no clear version, return False
        if len(version_payload) != 2:
            return False

        version, payload = version_payload

        # version must be a valid hex digit
        if not HEXDIGLC_RE.match(version):
            return False
        version = int(version, 16)

        # Version 255 is invalid
        # Only traceparent with at least 55 chars should be parsed
        if version == 255 or len(traceparent) < 55:
            return False

        fields = payload.split('-', 3)

        # Expect that there are at least 3 fields
        if len(fields) < 3:
            return False

        # Check field lengths and values
        for field, expected_length in zip(fields, (32, 16, 2)):
            if len(field) != expected_length or not HEXDIGLC_RE.match(field):
                return False

        trace_id, parent_id = fields[:2]
        self._trace_id = trace_id
        self.parent_span = parent_id
        if transport_type not in DISTRIBUTED_TRACE_TRANSPORT_TYPES:
            transport_type = 'Unknown'

        self.parent_transport_type = transport_type
        self._distributed_trace_state = ACCEPTED_DISTRIBUTED_TRACE
        return True

    def _parse_tracestate_header(self, tracestate):
        # Don't parse more than 32 entries
        entries = DELIMITER_FORMAT_RE.split(tracestate, 32)[:32]

        vendors = OrderedDict()
        for entry in entries:
            vendor_value = entry.split('=', 2)
            if len(vendor_value) != 2:
                continue

            vendor, value = vendor_value

            if len(vendor) > 256:
                continue

            if len(value) > 256:
                continue

            vendors[vendor] = value

        # Remove trusted new relic header if available and parse
        payload = vendors.pop(self._settings.trusted_account_key + '@nr', '')
        if not payload:
            self._record_supportability('Supportability/TraceContext/'
                    'TraceState/NoNrEntry')
        fields = payload.split('-', 9)
        if len(fields) >= 9:
            self.parent_type = PARENT_TYPE.get(fields[1])
            self.parent_account = fields[2]
            self.parent_app = fields[3]
            self.trusted_parent_span = fields[4]
            self.parent_tx = fields[5]
            if fields[6]:
                self._sampled = fields[6] == '1'
            if fields[7]:
                try:
                    self._priority = float(fields[7])
                except:
                    pass

            try:
                transport_start = int(fields[8]) / 1000.0
                now = time.time()
                if transport_start > now:
                    self.parent_transport_duration = 0.0
                else:
                    self.parent_transport_duration = now - transport_start
            except:
                pass

        self.tracing_vendors = ','.join(vendors.keys())

        if self._settings.span_events.enabled:
            self.tracestate = ','.join(
                    '{}={}'.format(k, v) for k, v in vendors.items())
        else:
            self.tracestate = tracestate

        return True

    def accept_distributed_trace_headers(self, headers, transport_type='HTTP'):
        if not self._can_accept_distributed_trace_headers():
            return False

        if self.settings.distributed_tracing.format == 'w3c':
            try:
                traceparent = headers.get('traceparent', '')
                tracestate = headers.get('tracestate', '')
            except Exception:
                traceparent = ''
                tracestate = ''

                for k, v in headers:
                    k = ensure_str(k)
                    if k == 'traceparent':
                        traceparent = v
                    elif k == 'tracestate':
                        tracestate = v

            try:
                _parent_parsed = self._parse_traceparent_header(
                        traceparent, transport_type)
            except:
                _parent_parsed = False

            if _parent_parsed:
                self._record_supportability('Supportability/TraceContext/'
                                        'TraceParent/Accept/Success')
                if tracestate:
                    tracestate = ensure_str(tracestate)
                    try:
                        _state_parsed = self._parse_tracestate_header(
                                tracestate)
                    except:
                        _state_parsed = False
                    if _state_parsed:
                        self._record_supportability(
                                'Supportability/TraceContext/'
                                'Accept/Success')
                    else:
                        self._record_supportability(
                                'Supportability/TraceContext/'
                                'TraceState/Parse/Exception')
            else:
                self._record_supportability('Supportability/TraceContext/'
                        'TraceParent/Parse/Exception')
        else:
            try:
                distributed_header = headers.get('newrelic')
            except Exception:
                for k, v in headers:
                    k = ensure_str(k)
                    if k == 'newrelic':
                        distributed_header = v
                        break
            distributed_header = ensure_str(distributed_header)
            if distributed_header is not None:
                return self._accept_distributed_trace_payload(
                        distributed_header,
                        transport_type)

    def _process_incoming_cat_headers(self, encoded_cross_process_id,
            encoded_txn_header):
        settings = self._settings

        if not self.enabled:
            return

        if not (settings.cross_application_tracer.enabled and
                settings.cross_process_id and settings.trusted_account_ids and
                settings.encoding_key):
            return

        if encoded_cross_process_id is None:
            return

        try:
            client_cross_process_id = deobfuscate(
                    encoded_cross_process_id, settings.encoding_key)

            # The cross process ID consists of the client
            # account ID and the ID of the specific application
            # the client is recording requests against. We need
            # to validate that the client account ID is in the
            # list of trusted account IDs and ignore it if it
            # isn't. The trusted account IDs list has the
            # account IDs as integers, so save the client ones
            # away as integers here so easier to compare later.

            client_account_id, client_application_id = \
                    map(int, client_cross_process_id.split('#'))

            if client_account_id not in settings.trusted_account_ids:
                return

            self.client_cross_process_id = client_cross_process_id
            self.client_account_id = client_account_id
            self.client_application_id = client_application_id

            txn_header = json_decode(deobfuscate(
                    encoded_txn_header,
                    settings.encoding_key))

            if txn_header:
                self.is_part_of_cat = True
                self.referring_transaction_guid = txn_header[0]

                # Incoming record_tt is OR'd with existing
                # record_tt. In the scenario where we make multiple
                # ext request, this will ensure we don't set the
                # record_tt to False by a later request if it was
                # set to True by an earlier request.

                self.record_tt = (self.record_tt or
                        txn_header[1])

                if isinstance(txn_header[2], six.string_types):
                    self._trip_id = txn_header[2]
                if isinstance(txn_header[3], six.string_types):
                    self._referring_path_hash = txn_header[3]
        except Exception:
            pass

    def _generate_response_headers(self, read_length=None):
        nr_headers = []

        # Generate metrics and response headers for inbound cross
        # process web external calls.

        if self.client_cross_process_id is not None:

            # Need to work out queueing time and duration up to this
            # point for inclusion in metrics and response header. If the
            # recording of the transaction had been prematurely stopped
            # via an API call, only return time up until that call was
            # made so it will match what is reported as duration for the
            # transaction.

            if self.queue_start:
                queue_time = self.start_time - self.queue_start
            else:
                queue_time = 0

            if self.end_time:
                duration = self.end_time - self.start_time
            else:
                duration = time.time() - self.start_time

            # Generate the additional response headers which provide
            # information back to the caller. We need to freeze the
            # transaction name before adding to the header.

            self._freeze_path()

            if read_length is None:
                read_length = self._read_length

            read_length = read_length if read_length is not None else -1

            payload = (self._settings.cross_process_id, self.path, queue_time,
                    duration, read_length, self.guid, self.record_tt)
            app_data = json_encode(payload)

            nr_headers.append(('X-NewRelic-App-Data', obfuscate(
                    app_data, self._settings.encoding_key)))

        return nr_headers

    def get_response_metadata(self):
        nr_headers = dict(self._generate_response_headers())
        return convert_to_cat_metadata_value(nr_headers)

    def process_request_metadata(self, cat_linking_value):
        try:
            payload = base64_decode(cat_linking_value)
        except:
            # `cat_linking_value` should always be able to be base64_decoded.
            # If this is encountered, the data being sent is corrupt. No
            # exception should be raised.
            return

        nr_headers = json_decode(payload)
        # TODO: All the external CAT APIs really need to
        # be refactored into the transaction class.
        encoded_cross_process_id = nr_headers.get('X-NewRelic-ID')
        encoded_txn_header = nr_headers.get('X-NewRelic-Transaction')
        return self._process_incoming_cat_headers(encoded_cross_process_id,
                encoded_txn_header)

    def set_transaction_name(self, name, group=None, priority=None):

        # Always perform this operation even if the transaction
        # is not active at the time as will be called from
        # constructor. If path has been frozen do not allow
        # name/group to be overridden. New priority then must be
        # same or greater than existing priority. If no priority
        # always override the existing name/group if not frozen.

        if self._name_priority is None:
            return

        if priority is not None and priority < self._name_priority:
            return

        if priority is not None:
            self._name_priority = priority

        # The name can be a URL for the default case. URLs are
        # supposed to be ASCII but can get a URL with illegal
        # non ASCII characters. As the rule patterns and
        # replacements are Unicode then can get Unicode
        # conversion warnings or errors when URL is converted to
        # Unicode and default encoding is ASCII. Thus need to
        # convert URL to Unicode as Latin-1 explicitly to avoid
        # problems with illegal characters.

        if isinstance(name, bytes):
            name = name.decode('Latin-1')

        # Deal with users who use group wrongly and add a leading
        # slash on it. This will cause an empty segment which we
        # want to avoid. In that case insert back in Function as
        # the leading segment.

        group = group or 'Function'

        if group.startswith('/'):
            group = 'Function' + group

        self._group = group
        self._name = name

    def record_exception(self, exc=None, value=None, tb=None,
                         params={}, ignore_errors=[]):

        # Bail out if the transaction is not active or
        # collection of errors not enabled.

        if not self._settings:
            return

        settings = self._settings
        error_collector = settings.error_collector

        if not error_collector.enabled:
            return

        if not settings.collect_errors and not settings.collect_error_events:
            return

        # If no exception details provided, use current exception.

        if exc is None and value is None and tb is None:
            exc, value, tb = sys.exc_info()

        # Has to be an error to be logged.

        if exc is None or value is None or tb is None:
            return

        # Where ignore_errors is a callable it should return a
        # tri-state variable with the following behavior.
        #
        #   True - Ignore the error.
        #   False- Record the error.
        #   None - Use the default ignore rules.

        should_ignore = None

        if callable(ignore_errors):
            should_ignore = ignore_errors(exc, value, tb)
            if should_ignore:
                return

        module = value.__class__.__module__
        name = value.__class__.__name__

        if should_ignore is None:
            # We need to check for module.name and module:name.
            # Originally we used module.class but that was
            # inconsistent with everything else which used
            # module:name. So changed to use ':' as separator, but
            # for backward compatibility need to support '.' as
            # separator for time being. Check that with the ':'
            # last as we will use that name as the exception type.

            if module:
                fullname = '%s.%s' % (module, name)
            else:
                fullname = name

            if not callable(ignore_errors) and fullname in ignore_errors:
                return

            if fullname in error_collector.ignore_errors:
                return

            if module:
                fullname = '%s:%s' % (module, name)
            else:
                fullname = name

            if not callable(ignore_errors) and fullname in ignore_errors:
                return

            if fullname in error_collector.ignore_errors:
                return

        else:
            if module:
                fullname = '%s:%s' % (module, name)
            else:
                fullname = name

        # Only remember up to limit of what can be caught for a
        # single transaction. This could be trimmed further
        # later if there are already recorded errors and would
        # go over the harvest limit.

        if len(self._errors) >= settings.agent_limits.errors_per_transaction:
            return

        # Only add params if High Security Mode is off.

        custom_params = {}

        if settings.high_security:
            if params:
                _logger.debug('Cannot add custom parameters in '
                        'High Security Mode.')
        else:
            try:
                for k, v in params.items():
                    name, val = process_user_attribute(k, v)
                    if name:
                        custom_params[name] = val
            except Exception:
                _logger.debug('Parameters failed to validate for unknown '
                        'reason. Dropping parameters for error: %r. Check '
                        'traceback for clues.', fullname, exc_info=True)
                custom_params = {}

        # Check to see if we need to strip the message before recording it.

        if (settings.strip_exception_messages.enabled and
                fullname not in settings.strip_exception_messages.whitelist):
            message = STRIP_EXCEPTION_MESSAGE
        else:
            try:

                # Favor unicode in exception messages.

                message = six.text_type(value)

            except Exception:
                try:

                    # If exception cannot be represented in unicode, this means
                    # that it is a byte string encoded with an encoding
                    # that is not compatible with the default system encoding.
                    # So, just pass this byte string along.

                    message = str(value)

                except Exception:
                    message = '<unprintable %s object>' % type(value).__name__

        # Check that we have not recorded this exception
        # previously for this transaction due to multiple
        # error traces triggering. This is not going to be
        # exact but the UI hides exceptions of same type
        # anyway. Better that we under count exceptions of
        # same type and message rather than count same one
        # multiple times.

        for error in self._errors:
            if error.type == fullname and error.message == message:
                return

        node = newrelic.core.error_node.ErrorNode(
                timestamp=time.time(),
                type=fullname,
                message=message,
                stack_trace=exception_stack(tb),
                custom_params=custom_params,
                file_name=None,
                line_number=None,
                source=None)

        # TODO Errors are recorded in time order. If
        # there are two exceptions of same type and
        # different message, the UI displays the first
        # one. In the PHP agent it was recording the
        # errors in reverse time order and so the UI
        # displayed the last one. What is the the
        # official order in which they should be sent.

        self._errors.append(node)

    def record_custom_metric(self, name, value):
        self._custom_metrics.record_custom_metric(name, value)

    def record_custom_metrics(self, metrics):
        for name, value in metrics:
            self._custom_metrics.record_custom_metric(name, value)

    def record_custom_event(self, event_type, params):
        settings = self._settings

        if not settings:
            return

        if not settings.custom_insights_events.enabled:
            return

        event = create_custom_event(event_type, params)
        if event:
            self._custom_events.add(event, priority=self.priority)

    def _intern_string(self, value):
        return self._string_cache.setdefault(value, value)

    def _process_node(self, node):
        self._trace_node_count += 1
        node.node_count = self._trace_node_count
        self.total_time += node.exclusive

        if type(node) is newrelic.core.database_node.DatabaseNode:
            settings = self._settings
            if not settings.collect_traces:
                return
            if (not settings.slow_sql.enabled and
                    not settings.transaction_tracer.explain_enabled):
                return
            if settings.transaction_tracer.record_sql == 'off':
                return
            if node.duration < settings.transaction_tracer.explain_threshold:
                return
            self._slow_sql.append(node)

    def stop_recording(self):
        if not self.enabled:
            return

        if self.stopped:
            return

        if self.end_time:
            return

        self.end_time = time.time()
        self.stopped = True

        if self._utilization_tracker:
            if self._thread_utilization_start:
                if not self._thread_utilization_end:
                    self._thread_utilization_end = (
                            self._utilization_tracker.utilization_count())

        self._cpu_user_time_end = os.times()[0]

    def add_custom_parameter(self, name, value):
        if not self._settings:
            return False

        if self._settings.high_security:
            _logger.debug('Cannot add custom parameter in High Security Mode.')
            return False

        if len(self._custom_params) >= MAX_NUM_USER_ATTRIBUTES:
            _logger.debug('Maximum number of custom attributes already '
                    'added. Dropping attribute: %r=%r', name, value)
            return False

        key, val = process_user_attribute(name, value)

        if key is None:
            return False
        else:
            self._custom_params[key] = val
            return True

    def add_custom_parameters(self, items):
        result = True

        # items is a list of (name, value) tuples.
        for name, value in items:
            result &= self.add_custom_parameter(name, value)

        return result

    def add_framework_info(self, name, version=None):
        if name:
            self._frameworks.add((name, version))

    def dump(self, file):
        """Dumps details about the transaction to the file object."""

        print('Application: %s' % (self.application.name), file=file)
        print('Time Started: %s' % (
                time.asctime(time.localtime(self.start_time))), file=file)
        print('Thread Id: %r' % (self.thread_id), file=file)
        print('Current Status: %d' % (self._state), file=file)
        print('Recording Enabled: %s' % (self.enabled), file=file)
        print('Ignore Transaction: %s' % (self.ignore_transaction), file=file)
        print('Transaction Dead: %s' % (self._dead), file=file)
        print('Transaction Stopped: %s' % (self.stopped), file=file)
        print('Background Task: %s' % (self.background_task), file=file)
        print('Request URI: %s' % (self._request_uri), file=file)
        print('Transaction Group: %s' % (self._group), file=file)
        print('Transaction Name: %s' % (self._name), file=file)
        print('Name Priority: %r' % (self._name_priority), file=file)
        print('Frozen Path: %s' % (self._frozen_path), file=file)
        print('AutoRUM Disabled: %s' % (self.autorum_disabled), file=file)
        print('Supress Apdex: %s' % (self.suppress_apdex), file=file)


def current_transaction(active_only=True):
    current = trace_cache().current_transaction()
    if active_only:
        if current and (current.ignore_transaction or current.stopped):
            return None
    return current


def set_transaction_name(name, group=None, priority=None):
    transaction = current_transaction()
    if transaction:
        transaction.set_transaction_name(name, group, priority)


def end_of_transaction():
    transaction = current_transaction()
    if transaction:
        transaction.stop_recording()


def set_background_task(flag=True):
    transaction = current_transaction()
    if transaction:
        transaction.background_task = flag


def ignore_transaction(flag=True):
    transaction = current_transaction()
    if transaction:
        transaction.ignore_transaction = flag


def suppress_apdex_metric(flag=True):
    transaction = current_transaction()
    if transaction:
        transaction.suppress_apdex = flag


def capture_request_params(flag=True):
    transaction = current_transaction()
    if transaction and transaction.settings:
        if transaction.settings.high_security:
            _logger.warn("Cannot modify capture_params in High Security Mode.")
        else:
            transaction.capture_params = flag


def add_custom_parameter(key, value):
    transaction = current_transaction()
    if transaction:
        return transaction.add_custom_parameter(key, value)
    else:
        return False


def add_custom_parameters(items):
    transaction = current_transaction()
    if transaction:
        return transaction.add_custom_parameters(items)
    else:
        return False


def add_framework_info(name, version=None):
    transaction = current_transaction()
    if transaction:
        transaction.add_framework_info(name, version)


def record_exception(exc=None, value=None, tb=None, params={},
        ignore_errors=[], application=None):
    if application is None:
        transaction = current_transaction()
        if transaction:
            transaction.record_exception(exc, value, tb, params,
                    ignore_errors)
    else:
        if application.enabled:
            application.record_exception(exc, value, tb, params,
                    ignore_errors)


def get_browser_timing_header():
    transaction = current_transaction()
    if transaction and hasattr(transaction, 'browser_timing_header'):
        return transaction.browser_timing_header()
    return ''


def get_browser_timing_footer():
    transaction = current_transaction()
    if transaction and hasattr(transaction, 'browser_timing_footer'):
        return transaction.browser_timing_footer()
    return ''


def disable_browser_autorum(flag=True):
    transaction = current_transaction()
    if transaction:
        transaction.autorum_disabled = flag


def suppress_transaction_trace(flag=True):
    transaction = current_transaction()
    if transaction:
        transaction.suppress_transaction_trace = flag


def record_custom_metric(name, value, application=None):
    if application is None:
        transaction = current_transaction()
        if transaction:
            transaction.record_custom_metric(name, value)
        else:
            _logger.debug('record_custom_metric has been called but no '
                'transaction was running. As a result, the following metric '
                'has not been recorded. Name: %r Value: %r. To correct this '
                'problem, supply an application object as a parameter to this '
                'record_custom_metrics call.', name, value)
    elif application.enabled:
        application.record_custom_metric(name, value)


def record_custom_metrics(metrics, application=None):
    if application is None:
        transaction = current_transaction()
        if transaction:
            transaction.record_custom_metrics(metrics)
        else:
            _logger.debug('record_custom_metrics has been called but no '
                'transaction was running. As a result, the following metrics '
                'have not been recorded: %r. To correct this problem, '
                'supply an application object as a parameter to this '
                'record_custom_metric call.', list(metrics))
    elif application.enabled:
        application.record_custom_metrics(metrics)


def record_custom_event(event_type, params, application=None):
    """Record a custom event.

    Args:
        event_type (str): The type (name) of the custom event.
        params (dict): Attributes to add to the event.
        application (newrelic.api.Application): Application instance.

    """

    if application is None:
        transaction = current_transaction()
        if transaction:
            transaction.record_custom_event(event_type, params)
        else:
            _logger.debug('record_custom_event has been called but no '
                'transaction was running. As a result, the following event '
                'has not been recorded. event_type: %r params: %r. To correct '
                'this problem, supply an application object as a parameter to '
                'this record_custom_event call.', event_type, params)
    elif application.enabled:
        application.record_custom_event(event_type, params)


def accept_distributed_trace_payload(payload, transport_type='HTTP'):
    transaction = current_transaction()
    if transaction:
        return transaction.accept_distributed_trace_payload(payload,
                transport_type)
    return False


def accept_distributed_trace_headers(headers, transport_type='HTTP'):
    transaction = current_transaction()
    if transaction:
        return transaction.accept_distributed_trace_headers(
                headers,
                transport_type)


def create_distributed_trace_payload():
    transaction = current_transaction()
    if transaction:
        return transaction.create_distributed_trace_payload()


def insert_distributed_trace_headers(headers):
    transaction = current_transaction()
    if transaction:
        return transaction.insert_distributed_trace_headers(headers)


def current_trace_id():
    transaction = current_transaction()
    if transaction:
        return transaction.trace_id


def current_span_id():
    trace = trace_cache().current_trace()
    if trace:
        return trace.guid
