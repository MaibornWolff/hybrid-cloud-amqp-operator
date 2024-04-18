from datetime import datetime, timezone
import kopf
from .routing import amqp_backend
from hybridcloud_core.configuration import config_get
from hybridcloud_core.operator.reconcile_helpers import ignore_control_label_change, process_action_label
from hybridcloud_core.k8s.api import get_namespaced_custom_object, patch_namespaced_custom_object_status, create_or_update_secret, get_secret, delete_secret
from ..util import k8s
from ..util.constants import BACKOFF


if config_get("handler_on_resume", default=False):
    @kopf.on.resume(*k8s.AMQPBroker.kopf_on(), backoff=BACKOFF)
    def broker_resume(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
        broker_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs)


@kopf.on.create(*k8s.AMQPBroker.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.AMQPBroker.kopf_on(), backoff=BACKOFF)
def broker_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
    if ignore_control_label_change(diff):
        logger.debug("Only control labels removed. Nothing to do.")
        return
        
    # Wait for topic
    topic_namespace = spec["topicRef"].get("namespace", namespace)
    backend, backend_name, broker_name, topic_name, allowed_k8s_namespaces = _wait_for_topic(logger, topic_namespace, spec["topicRef"]["name"], retry)

    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = spec.get("backend", config_get("backend", fail_if_missing=True))
    backend = amqp_backend(backend_name, logger)

    valid, reason = backend.broker_spec_valid(namespace, name, spec)
    if not valid:
        _status(name, namespace, status, "failed", f"Validation failed: {reason}")
        raise kopf.PermanentError("Spec is invalid, check status for details")

    # Create broker
    broker_name = backend.create_or_update_broker(namespace, name, spec)
    
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
    }, body, k8s.AMQPBroker)

    # Generate credentials
    if not credentials_secret:
        credentials = backend.create_or_update_broker_credentials(broker_name, topic_name, broker_name, reset_credentials)
        create_or_update_secret(namespace, spec["credentialsSecret"], credentials)

    # mark success
    _status(name, namespace, status, "finished", "Broker created", backend=backend_name, broker_name=broker_name)


@kopf.on.delete(*k8s.AMQPBroker.kopf_on(), backoff=BACKOFF)
def broker_delete(spec, status, name, namespace, logger, **kwargs):
    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = config_get("backend", fail_if_missing=True)
    backend = amqp_backend(backend_name, logger)
    broker_name = status["broker_name"]
    topic_name = status["topic_name"]
    subscription_name = status["subscription_name"]
    delete_secret(namespace, spec["credentialsSecret"])

    if backend.broker_exists(namespace, name):
        backend.delete_broker(namespace, name)
    backend.delete_broker_credentials(subscription_name, topic_name, broker_name)
    

def _status(name, namespace, status_obj, status, reason=None, backend=None, endpoint=None, broker_name=None):
    if status_obj:
        new_status = dict()
        for k, v in status_obj.items():
            new_status[k] = v
        status_obj = new_status
    else:
        status_obj = dict()
    if backend:
        status_obj["backend"] = backend
    if endpoint:
        status_obj["endpoint"] = endpoint
    if broker_name:
        status_obj["broker_name"] = broker_name
    status_obj["deployment"] = {
        "status": status,
        "reason": reason,
        "latest-update": datetime.now(tz=timezone.utc).isoformat()
    }
    patch_namespaced_custom_object_status(k8s.AMQPBroker, namespace, name, status_obj)

def _wait_for_topic(logger, topic_namespace, topic_name, retry):
    topic_object = get_namespaced_custom_object(k8s.AMQPBroker, topic_namespace, topic_name)
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
