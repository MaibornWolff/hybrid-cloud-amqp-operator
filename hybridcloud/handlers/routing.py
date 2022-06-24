from ..backends.azureservicebus import AzureServiceBusBackend
from ..backends.rabbitmq import RabbitMQBackend
from hybridcloud_core.configuration import config_get, ConfigurationException


_backends = {
    "azureservicebus": AzureServiceBusBackend,
    "rabbitmq": RabbitMQBackend,
}


def amqp_backend(selected_backend, logger) -> AzureServiceBusBackend:
    backend = config_get("backend", fail_if_missing=True)
    if backend not in _backends.keys():
        raise ConfigurationException(f"Unknown backend: {backend}")
    if selected_backend:
        if selected_backend not in _backends.keys():
            logger.warn(f"Selected backend {selected_backend} is unknown. Defaulting to {backend}")
            selected_backend = backend
    else:
        selected_backend = backend
    return _backends[selected_backend](logger)
