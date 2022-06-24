import kopf
from .routing import amqp_backend
from hybridcloud_core.configuration import config_get
from hybridcloud_core.operator.reconcile_helpers import ignore_control_label_change, process_action_label
from hybridcloud_core.k8s.api import get_namespaced_custom_object, patch_namespaced_custom_object_status, create_or_update_secret, get_secret, delete_secret
from ..util import k8s
from ..util.constants import BACKOFF


if config_get("handler_on_resume", default=False):
    @kopf.on.resume(*k8s.AMQPQueueConsumer.kopf_on(), backoff=BACKOFF)
    def queue_consumer_resume(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
        queue_consumer_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs)


@kopf.on.create(*k8s.AMQPQueueConsumer.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.AMQPQueueConsumer.kopf_on(), backoff=BACKOFF)
def queue_consumer_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
    if ignore_control_label_change(diff):
        logger.debug("Only control labels removed. Nothing to do.")
        return

    # Wait for queue
    queue_namespace = spec["queueRef"].get("namespace", namespace)
    backend, backend_name, broker_name, queue_name, allowed_k8s_namespaces = _wait_for_queue(logger, queue_namespace, spec["queueRef"]["name"], retry)

    # Check for cross-namespace
    if queue_namespace != namespace:
        if not config_get("cross_namespace.allow_consume", default=False):
            _status(name, namespace, status, "failed", "Queue and Consumer in different k8s namespaces is not allowed")
            raise kopf.PermanentError("Queue and Consumer in different k8s namespaces is not allowed")
        if not namespace in allowed_k8s_namespaces:
            _status(name, namespace, status, "failed", "Your k8s namespace is not allowed to use the referenced AMQPQueue")
            raise kopf.PermanentError(f"Your k8s namespace is not allowed to use the referenced AMQPQueue")  

    # Validate spec
    valid, reason = backend.queue_consumer_spec_valid(namespace, name, spec)
    if not valid:
        _status(name, namespace, status, "failed", f"Validation failed: {reason}")
        raise kopf.PermanentError("Spec is invalid, check status for details")

    _status(name, namespace, status, "working", backend=backend_name, queue_name=queue_name, broker_name=broker_name)


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
    }, body, k8s.AMQPQueueConsumer)

    # Generate credentials
    if not credentials_secret:
        credentials = backend.create_or_update_queue_consumer_credentials(namespace, name, queue_name, broker_name, reset_credentials)
        create_or_update_secret(namespace, spec["credentialsSecret"], credentials)

    # mark success
    _status(name, namespace, status, "finished", "QueueConsumer created", backend=backend_name, broker_name=broker_name, queue_name=queue_name)


@kopf.on.delete(*k8s.AMQPQueueConsumer.kopf_on(), backoff=BACKOFF)
def queue_consumer_delete(spec, status, name, namespace, logger, **kwargs):
    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = config_get("backend", fail_if_missing=True)
    backend = amqp_backend(backend_name, logger)
    if not status or not "broker_name" in status or not "queue_name" in status:
        logger.warn("Could not delete QueueConsumer as no namespace and queue information was stored in status")
        return
    broker_name = status["broker_name"]
    queue_name = status["queue_name"]

    delete_secret(namespace, spec["credentialsSecret"])
    backend.delete_queue_consumer_credentials(namespace, name, queue_name, broker_name)


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
    patch_namespaced_custom_object_status(k8s.AMQPQueueConsumer, namespace, name, status_obj)


def _wait_for_queue(logger, queue_namespace, queue_name, retry):
    queue_object = get_namespaced_custom_object(k8s.AMQPQueue, queue_namespace, queue_name)
    if not queue_object:
        raise kopf.TemporaryError("Waiting for queue to be created.", delay=10 if retry < 5 else 20 if retry < 10 else 30)

    status = queue_object.get("status")
    if not status or not "broker_name" in status or not "queue_name" in status:
        raise kopf.TemporaryError("Waiting for queue to be created.", delay=10 if retry < 5 else 20 if retry < 10 else 30)
    backend_name = status.get("backend", queue_object.get("spec", dict()).get("backend", config_get("backend", fail_if_missing=True)))
    backend = amqp_backend(backend_name, logger)
    broker_name = status["broker_name"]

    queue_exists = backend.queue_exists(queue_namespace, queue_name, broker_name)
    if not queue_exists:
        raise kopf.TemporaryError("Waiting for queue to be created.", delay=10 if retry < 5 else 20 if retry < 10 else 30)
    return backend, backend_name, status["broker_name"], status["queue_name"], queue_object["spec"].get("allowedK8sNamespaces", [])
