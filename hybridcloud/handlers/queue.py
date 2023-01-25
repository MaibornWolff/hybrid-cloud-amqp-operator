import kopf
from .routing import amqp_backend
from hybridcloud_core.configuration import config_get
from hybridcloud_core.operator.reconcile_helpers import ignore_control_label_change, process_action_label
from hybridcloud_core.k8s.api import patch_namespaced_custom_object_status, create_or_update_secret, get_secret, delete_secret
from ..util import k8s
from ..util.constants import BACKOFF
from .helpers import wait_for_amqp_broker

from ..util.metrics import (
    PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER, 
    PROMETHEUS_HANDLER_EXCEPTION_COUNTER, 
    PROMETHEUS_RESOURCES_CREATED_TOTAL_GAUGE,
    ACTIONS,
    extract_count_from_kopf_index, 
    initialize_prometheus_handler_metrics, 
    initialize_prometheus_resource_gauge
)

_HANDLER_NAME = "queue"
_RESOURCE_TYPE = "AMQPQueue"
initialize_prometheus_handler_metrics(_HANDLER_NAME)
initialize_prometheus_resource_gauge(_RESOURCE_TYPE)


@kopf.on.create(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
@kopf.on.delete(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
def set_resource_count_metric(resource_index: kopf.Index, **_):
    count = extract_count_from_kopf_index(resource_index, _RESOURCE_TYPE)
    PROMETHEUS_RESOURCES_CREATED_TOTAL_GAUGE.labels(type=_RESOURCE_TYPE).set(count)


if config_get("handler_on_resume", default=False):
    @kopf.on.resume(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
    def queue_resume(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
        _ACTION_NAME = ACTIONS.RESUME
        PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME).inc()

        with (PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME)).count_exceptions():
            queue_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs)


@kopf.on.create(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
def queue_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
    _ACTION_NAME = ACTIONS.CREATE_OR_UPDATE
    PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=_HANDLER_NAME, action="create_or_update").inc()

    with (PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME)).count_exceptions():
        if ignore_control_label_change(diff):
            logger.debug("Only control labels removed. Nothing to do.")
            return

        # Wait for broker
        broker_namespace = spec["brokerRef"].get("namespace", namespace)
        backend, backend_name, broker_name, allowed_k8s_namespaces = wait_for_amqp_broker(logger, broker_namespace, spec["brokerRef"]["name"], retry)

        # Check for cross-namespace
        if broker_namespace != namespace:
            if not config_get("cross_namespace.allow_produce", default=False):
                _status(name, namespace, status, "failed", f"AMQPBroker and AMQPQueue in different k8s namespaces is not allowed")
                raise kopf.PermanentError("AMQPBroker and AMQPQueue in different k8s namespaces is not allowed")
            if not namespace in allowed_k8s_namespaces:
                _status(name, namespace, status, "failed", f"Your k8s namespace is not allowed to use the referenced AMQPBroker")
                raise kopf.PermanentError(f"Your k8s namespace is not allowed to use the referenced AMQPBroker")  

        # Validate spec
        valid, reason = backend.queue_spec_valid(namespace, name, spec, broker_name)
        if not valid:
            _status(name, namespace, status, "failed", f"Validation failed: {reason}")
            raise kopf.PermanentError("Spec is invalid, check status for details")

        _status(name, namespace, status, "working", backend=backend_name, broker_name=broker_name)

        # Create queue
        queue_name = backend.create_or_update_queue(namespace, name, spec, broker_name)

        credentials_secret = get_secret(namespace, spec["credentialsSecret"])
        reset_credentials = False

        def action_reset_credentials():
            nonlocal credentials_secret
            nonlocal reset_credentials
            credentials_secret = None
            reset_credentials = True
            return "Credentials reset"
        process_action_label(labels, {
            "reset-credentials": action_reset_credentials,
        }, body, k8s.AMQPQueue)

        # Generate credentials
        if not credentials_secret:
            credentials = backend.create_or_update_queue_credentials(queue_name, broker_name, reset_credentials)
            create_or_update_secret(namespace, spec["credentialsSecret"], credentials)

        # mark success
        _status(name, namespace, status, "finished", "Queue created", backend=backend_name, broker_name=broker_name, queue_name=queue_name)


@kopf.on.delete(*k8s.AMQPQueue.kopf_on(), backoff=BACKOFF)
def queue_delete(spec, status, name, namespace, logger, **kwargs):
    _ACTION_NAME = ACTIONS.DELETE
    PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME).inc()

    with (PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME)).count_exceptions():
        if status and "backend" in status:
            backend_name = status["backend"]
        else:
            backend_name = config_get("backend", fail_if_missing=True)
        backend = amqp_backend(backend_name, logger)
        if not status or not "broker_name" in status:
            logger.warn("Could not delete queue as no broker information was stored in status")
            return
        broker_name = status["broker_name"]

        delete_secret(namespace, spec["credentialsSecret"])

        if backend.queue_exists(namespace, name, broker_name):
            backend.delete_queue(namespace, name, broker_name)


def _status(name, namespace, status_obj, status, reason=None, backend=None, broker_name=None, queue_name=None):
    if status_obj:
        new_status = dict()
        for k, v in status_obj.items():
            new_status[k] = v
        status_obj = new_status
    else:
        status_obj = dict()
    if backend:
        status_obj["backend"] = backend
    if broker_name:
        status_obj["broker_name"] = broker_name
    if queue_name:
        status_obj["queue_name"] = queue_name
    status_obj["deployment"] = {
        "status": status,
        "reason": reason
    }
    patch_namespaced_custom_object_status(k8s.AMQPQueue, namespace, name, status_obj)
