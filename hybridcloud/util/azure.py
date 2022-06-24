from azure.identity import DefaultAzureCredential
from azure.mgmt.servicebus.v2021_06_01_preview import ServiceBusManagementClient
from hybridcloud_core.configuration import get_one_of


def _subscription_id():
    return get_one_of("backends.azureservicebus.subscription_id", "backends.azure.subscription_id", fail_if_missing=True)


def _credentials():
    return DefaultAzureCredential()


def servicebus_client() -> ServiceBusManagementClient:
    return ServiceBusManagementClient(_credentials(), _subscription_id())
