import kopf
from .routing import amqp_backend
from hybridcloud_core.configuration import config_get
from hybridcloud_core.operator.reconcile_helpers import ignore_control_label_change, process_action_label
from hybridcloud_core.k8s.api import get_namespaced_custom_object, patch_namespaced_custom_object_status, create_or_update_secret, get_secret, delete_secret
from ..util import k8s
from ..util.constants import BACKOFF
from ..util.metrics import (
    PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER, 
    PROMETHEUS_HANDLER_EXCEPTION_COUNTER, 
    PROMETHEUS_RESOURCES_CREATED_TOTAL_GAUGE,
    ACTIONS,
    extract_count_from_kopf_index, 
    initialize_prometheus_handler_metrics, 
    initialize_prometheus_resource_gauge
)

_HANDLER_NAME = "topic_subscription"
_RESOURCE_TYPE = "AMQPTopicSubscription"
initialize_prometheus_handler_metrics(_HANDLER_NAME)
initialize_prometheus_resource_gauge(_RESOURCE_TYPE)


@kopf.on.create(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
@kopf.on.delete(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
def set_resource_count_metric(resource_index: kopf.Index, **_):
    count = extract_count_from_kopf_index(resource_index, _RESOURCE_TYPE)
    PROMETHEUS_RESOURCES_CREATED_TOTAL_GAUGE.labels(type=_RESOURCE_TYPE).set(count)


if config_get("handler_on_resume", default=False):
    @kopf.on.resume(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
    def topic_subscription_resume(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
        _ACTION_NAME = ACTIONS.RESUME
        PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME).inc()
        
        with (PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME)).count_exceptions():
            topic_subscription_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs)


@kopf.on.create(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
def topic_subscription_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
    _ACTION_NAME = ACTIONS.CREATE_OR_UPDATE
    PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME).inc()

    with (PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME)).count_exceptions():
        if ignore_control_label_change(diff):
            logger.debug("Only control labels removed. Nothing to do.")
            return

        # Wait for topic
        topic_namespace = spec["topicRef"].get("namespace", namespace)
        backend, backend_name, broker_name, topic_name, allowed_k8s_namespaces = _wait_for_topic(logger, topic_namespace, spec["topicRef"]["name"], retry)

        # Check for cross-namespace
        if topic_namespace != namespace:
            if not config_get("cross_namespace.allow_consume", default=False):
                _status(name, namespace, status, "failed", "Topic and Subscription in different k8s namespaces is not allowed")
                raise kopf.PermanentError("Topic and Subscription in different k8s namespaces is not allowed")
            if not namespace in allowed_k8s_namespaces:
                _status(name, namespace, status, "failed", "Your k8s namespace is not allowed to use the referenced AMQPTopic")
                raise kopf.PermanentError(f"Your k8s namespace is not allowed to zse the referenced AMQPTopic")  

        # Validate spec
        valid, reason = backend.topic_subscription_spec_valid(namespace, name, spec)
        if not valid:
            _status(name, namespace, status, "failed", f"Validation failed: {reason}")
            raise kopf.PermanentError("Spec is invalid, check status for details")

        _status(name, namespace, status, "working", backend=backend_name, topic_name=topic_name, broker_name=broker_name)

        # Create topic subscription
        subscription_name = backend.create_or_update_topic_subscription(namespace, name, spec, topic_name, broker_name)

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
        }, body, k8s.AMQPTopicSubscription)

        # Generate credentials
        if not credentials_secret:
            credentials = backend.create_or_update_topic_subscription_credentials(subscription_name, topic_name, broker_name, reset_credentials)
            create_or_update_secret(namespace, spec["credentialsSecret"], credentials)

        # mark success
        _status(name, namespace, status, "finished", "TopicSubscription created", backend=backend_name, broker_name=broker_name, topic_name=topic_name, subscription_name=subscription_name)


@kopf.on.delete(*k8s.AMQPTopicSubscription.kopf_on(), backoff=BACKOFF)
def topic_subscription_delete(spec, status, name, namespace, logger, **kwargs):
    _ACTION_NAME = ACTIONS.DELETE
    PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME).inc()

    with (PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=_HANDLER_NAME, action=_ACTION_NAME)).count_exceptions():
        if status and "backend" in status:
            backend_name = status["backend"]
        else:
            backend_name = config_get("backend", fail_if_missing=True)
        backend = amqp_backend(backend_name, logger)
        if not status or not "broker_name" in status or not "topic_name" in status or not "subscription_name" in status:
            logger.warn("Could not delete topic scubscription as no broker and topic information was stored in status")
            return
        broker_name = status["broker_name"]
        topic_name = status["topic_name"]
        subscription_name = status["subscription_name"]

        delete_secret(namespace, spec["credentialsSecret"])

        if backend.topic_subscription_exists(namespace, name, topic_name, broker_name):
            backend.delete_topic_subscription(namespace, name, topic_name, broker_name)
        backend.delete_topic_subscription_credentials(subscription_name, topic_name, broker_name)


def _status(name, namespace, status_obj, status, reason=None, backend=None, broker_name=None, topic_name=None, subscription_name=None):
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
    if topic_name:
        status_obj["topic_name"] = topic_name
    if subscription_name:
        status_obj["subscription_name"] = subscription_name
    status_obj["deployment"] = {
        "status": status,
        "reason": reason
    }
    patch_namespaced_custom_object_status(k8s.AMQPTopicSubscription, namespace, name, status_obj)


def _wait_for_topic(logger, topic_namespace, topic_name, retry):
    topic_object = get_namespaced_custom_object(k8s.AMQPTopic, topic_namespace, topic_name)
    if not topic_object:
        raise kopf.TemporaryError("Waiting for topic object to be created.", delay=10 if retry < 5 else 20 if retry < 10 else 30)

    status = topic_object.get("status")
    if not status or not "broker_name" in status or not "topic_name" in status:
        raise kopf.TemporaryError("Waiting for topic to be created by backend.", delay=10 if retry < 5 else 20 if retry < 10 else 30)
    backend_name = status.get("backend", topic_object.get("spec", dict()).get("backend", config_get("backend", fail_if_missing=True)))
    backend = amqp_backend(backend_name, logger)
    broker_name = status["broker_name"]

    topic_exists = backend.topic_exists(topic_namespace, topic_name, broker_name)
    if not topic_exists:
        raise kopf.TemporaryError("Waiting for topic to be finished creating by backend.", delay=10 if retry < 5 else 20 if retry < 10 else 30)
    return backend, backend_name, status["broker_name"], status["topic_name"], topic_object["spec"].get("allowedK8sNamespaces", [])
