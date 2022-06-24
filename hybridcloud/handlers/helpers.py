import kopf
from hybridcloud_core.configuration import config_get
from hybridcloud_core.k8s.api import get_namespaced_custom_object
from ..util import k8s
from .routing import amqp_backend


def wait_for_amqp_broker(logger, broker_namespace, broker_name, retry):
    broker_object = get_namespaced_custom_object(k8s.AMQPBroker, broker_namespace, broker_name)
    if not broker_object:
        raise kopf.TemporaryError("Waiting for broker object to be created.", delay=10 if retry < 5 else 20 if retry < 10 else 30)

    status = broker_object.get("status")
    if not status or not "broker_name" in status:
        raise kopf.TemporaryError("Waiting for broker to be created by backend.", delay=10 if retry < 5 else 20 if retry < 10 else 30)
    backend_name = status.get("backend", broker_object.get("spec", dict()).get("backend", config_get("backend", fail_if_missing=True)))
    backend = amqp_backend(backend_name, logger)

    if not backend.broker_exists(broker_namespace, broker_name):
        raise kopf.TemporaryError("Waiting for broker to be finished creating by backend.", delay=10 if retry < 5 else 20 if retry < 10 else 30)
    return backend, backend_name, status["broker_name"], broker_object.get("spec", dict()).get("allowedK8sNamespaces", [])
