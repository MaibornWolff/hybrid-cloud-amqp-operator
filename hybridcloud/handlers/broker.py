import kopf
from .routing import amqp_backend
from hybridcloud_core.configuration import config_get
from hybridcloud_core.operator.reconcile_helpers import ignore_control_label_change
from hybridcloud_core.k8s.api import patch_namespaced_custom_object_status
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

    # mark success
    _status(name, namespace, status, "finished", "Broker created", backend=backend_name, broker_name=broker_name)


@kopf.on.delete(*k8s.AMQPBroker.kopf_on(), backoff=BACKOFF)
def broker_delete(spec, status, name, namespace, logger, **kwargs):
    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = config_get("backend", fail_if_missing=True)
    backend = amqp_backend(backend_name, logger)
    if backend.broker_exists(namespace, name):
        backend.delete_broker(namespace, name)


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
        "reason": reason
    }
    patch_namespaced_custom_object_status(k8s.AMQPBroker, namespace, name, status_obj)
