import base64
from datetime import timedelta
import hashlib
import hmac
import urllib
import string
import time
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.servicebus.v2021_06_01_preview.models import CheckNameAvailability, SBNamespace, SBSku, SBTopic, SBAuthorizationRule, RegenerateAccessKeyParameters, SBSubscription, AccessRights, SBQueue
from hybridcloud_core.configuration import get_one_of
from hybridcloud_core.operator.reconcile_helpers import field_from_spec
from ..util.azure import servicebus_client


ALLOWED_NAMESPACE_NAME_CHARACTERS = string.ascii_lowercase + string.digits + "-"
TAG_PREFIX = "hybridcloud-amqp-operator"


def _backend_config(key, default=None, fail_if_missing=False):
    return get_one_of(f"backends.azureservicebus.{key}", f"backends.azure.{key}", default=default, fail_if_missing=fail_if_missing)


def _calc_namespace_name(namespace, name):
    return _backend_config("name_pattern_namespace", fail_if_missing=True).format(namespace=namespace, name=name).lower()


def _calc_topic_name(namespace, name):
    return _backend_config("topic.name_pattern", default="{namespace}-{name}").format(namespace=namespace, name=name).lower()


def _calc_queue_name(namespace, name):
    return _backend_config("queue.name_pattern", default="{namespace}-{name}").format(namespace=namespace, name=name).lower()


def _calc_subscription_name(namespace, name):
    return _backend_config("topic.name_pattern_subscription", default="{namespace}-{name}").format(namespace=namespace, name=name).lower()


def _calc_queue_consumer_name(namespace, name):
    return _backend_config("queue.name_pattern_consumer", default="{namespace}-{name}").format(namespace=namespace, name=name).lower()


class AzureServiceBusBackend:
    def __init__(self, logger):
        self._logger = logger
        self._servicebus_client = servicebus_client()
        self._subscription_id = _backend_config("subscription_id", fail_if_missing=True)
        self._location = _backend_config("location", fail_if_missing=True)
        self._resource_group = _backend_config("resource_group", fail_if_missing=True)

    def broker_spec_valid(self, namespace, name, spec):
        namespace_name = _calc_namespace_name(namespace, name)
        if len(namespace_name) > 50:
            return (False, f"calculated broker name '{namespace_name}' is longer than 50 characters")
        for char in namespace_name:
            if char not in ALLOWED_NAMESPACE_NAME_CHARACTERS:
                return (False, f"Character '{char}' is not allowed in name. Allowed are: letters, digits and hyphens")
        # Check if name is available
        if not self.broker_exists(namespace, name):
            result = self._servicebus_client.namespaces.check_name_availability(CheckNameAvailability(name=namespace_name))
            if not result.name_available:
                return (False, f"Name for servicebus namespace cannot be used: {result.reason}: {result.message}")
        return (True, "")

    def broker_exists(self, namespace, name):
        try:
            return self._servicebus_client.namespaces.get(self._resource_group, _calc_namespace_name(namespace, name))
        except ResourceNotFoundError:
            return False

    def create_or_update_broker(self, namespace, name, spec, extra_tags=None):
        namespace_name = _calc_namespace_name(namespace, name)
        sku = _backend_config("sku", default="Basic")
        capacity = _backend_config("capacity")
        if capacity:
            capacity = int(capacity)
        parameters = SBNamespace(
            location=self._location,
            tags=_tags(namespace, name, extra_tags),
            sku=SBSku(name=sku, tier=sku, capacity=capacity)
        )
        existing_namespace = self.broker_exists(namespace, name)
        if not existing_namespace or existing_namespace.sku.name != parameters.sku.name or existing_namespace.sku.capacity != parameters.sku.capacity:
            namespace = self._servicebus_client.namespaces.begin_create_or_update(self._resource_group, namespace_name, parameters).result()

        return namespace_name

    def delete_broker(self, namespace, name):
        namespace_name = _calc_namespace_name(namespace, name)
        fake_delete = _backend_config("fake_delete", default=False)
        if fake_delete:
            self.create_or_update_broker(namespace, name, None, {"marked-for-deletion": "yes"})
        else:
            self._servicebus_client.namespaces.begin_delete(self._resource_group, namespace_name).result()

    def topic_spec_valid(self, namespace, name, spec, broker_name):
        if not self.topic_exists(namespace, name, broker_name):
            if self.queue_exists(namespace, name, broker_name):
                return (False, "There is already a queue with the same name")
        topic_name = _calc_topic_name(namespace, name)
        if len(topic_name) > 260:
            return (False, f"calculated topic name '{topic_name}' is longer than 260 characters")
        for char in topic_name:
            if char not in ALLOWED_NAMESPACE_NAME_CHARACTERS:
                return (False, f"Character '{char}' is not allowed in name. Allowed are: letters, digits and hyphens")
        return True, ""

    def topic_exists(self, namespace, name, broker_name):
        topic_name = _calc_topic_name(namespace, name)
        try:
            return self._servicebus_client.topics.get(self._resource_group, broker_name, topic_name)
        except ResourceNotFoundError:
            return False

    def create_or_update_topic(self, namespace, name, spec, namespace_name):
        topic_name = _calc_topic_name(namespace, name)
        default_message_ttl = field_from_spec(spec, "topic.defaultTTLSeconds", _backend_config("topic.parameters.default_ttl_seconds", default=60*60*24*30))
        if default_message_ttl:
            default_message_ttl = timedelta(seconds=int(default_message_ttl))
        parameters = SBTopic(
            default_message_time_to_live=default_message_ttl,
            max_size_in_megabytes=_backend_config("topic.parameters.max_size_in_megabytes", default=None),
            support_ordering=_backend_config("topic.parameters.support_ordering", default=False)
        )
        existing_topic = self.topic_exists(namespace, name, namespace_name)
        def diff():
            if existing_topic.default_message_time_to_live != parameters.default_message_time_to_live:
                return True
            if existing_topic.max_size_in_megabytes != parameters.max_size_in_megabytes:
                return True
            if existing_topic.support_ordering != parameters.support_ordering:
                return True
            return False
        if not existing_topic or diff():
            self._servicebus_client.topics.create_or_update(self._resource_group, namespace_name, topic_name, parameters)
        return topic_name

    def delete_topic(self, namespace, name, namespace_name):
        topic_name = _calc_topic_name(namespace, name)
        fake_delete = _backend_config("topic.fake_delete", default=False)
        if not fake_delete:
            self._servicebus_client.topics.delete(self._resource_group, namespace_name, topic_name)

    def create_or_update_topic_credentials(self, topic_name, namespace_name, reset_credentials=False):
        return self._create_or_update_topic_credentials(f"{topic_name}-owner", topic_name, namespace_name, [AccessRights.MANAGE, AccessRights.LISTEN, AccessRights.SEND], topic_name, reset_credentials)

    def delete_topic_credentials(self, topic_name, namespace_name):
        try:
            self._servicebus_client.topics.delete_authorization_rule(self._resource_group, namespace_name, topic_name, f"{topic_name}-owner")
        except ResourceNotFoundError:
            pass

    def topic_subscription_exists(self, namespace, name, topic_name, namespace_name):
        subscription_name = _calc_subscription_name(namespace, name)
        try:
            return self._servicebus_client.subscriptions.get(self._resource_group, namespace_name, topic_name, subscription_name)
        except ResourceNotFoundError:
            return False

    def create_or_update_topic_subscription(self, namespace, name, spec, topic_name, namespace_name):
        subscription_name = _calc_subscription_name(namespace, name)
        default_message_ttl = field_from_spec(spec, "subscription.defaultTTLSeconds", _backend_config("subscription.parameters.default_ttl_seconds", default=60*60*24*30))
        if default_message_ttl:
            default_message_ttl = timedelta(seconds=int(default_message_ttl))
        lock_duration = field_from_spec(spec, "subscription.lockDurationSeconds", _backend_config("subscription.parameters.lock_duration_seconds", default=60))
        if lock_duration:
            lock_duration = timedelta(seconds=int(lock_duration))
        parameters = SBSubscription(
            default_message_time_to_live=default_message_ttl,
            lock_duration=lock_duration,
            dead_lettering_on_message_expiration=field_from_spec(spec, "subscription.enableDeadLettering", _backend_config("subscription.parameters.dead_lettering_on_message_expiration", default=False)),
            max_delivery_count=int(field_from_spec(spec, "subscription.maxDeliveryCount", _backend_config("subscription.parameters.max_delivery_count", default=10))),
        )
        self._servicebus_client.subscriptions.create_or_update(self._resource_group, namespace_name, topic_name, subscription_name, parameters)
        return subscription_name

    def delete_topic_subscription(self, namespace, name, topic_name, namespace_name):
        subscription_name = _calc_subscription_name(namespace, name)
        self._servicebus_client.subscriptions.delete(self._resource_group, namespace_name, topic_name, subscription_name)

    def create_or_update_topic_subscription_credentials(self, subscription_name, topic_name, namespace_name, reset_credentials=False):
        return self._create_or_update_topic_credentials(subscription_name, topic_name, namespace_name, [AccessRights.LISTEN], f"{topic_name}/Subscriptions/{subscription_name}", reset_credentials)

    def delete_topic_subscription_credentials(self, subscription_name, topic_name, namespace_name):
        try:
            self._servicebus_client.topics.delete_authorization_rule(self._resource_group, namespace_name, topic_name, subscription_name)
        except ResourceNotFoundError:
            pass

    def topic_subscription_spec_valid(self, namespace, name, spec):
        subscription_name = _calc_subscription_name(namespace, name)
        if len(subscription_name) > 50:
            return (False, f"calculated subscription name '{subscription_name}' is longer than 50 characters")
        for char in subscription_name:
            if char not in ALLOWED_NAMESPACE_NAME_CHARACTERS:
                return (False, f"Character '{char}' is not allowed in name. Allowed are: letters, digits and hyphens")
        return (True, "")

    def _create_or_update_topic_credentials(self, token_name, topic_name, namespace_name, permissions, entity_path, reset_credentials=False):
        # Create or update authorization rule
        parameters = SBAuthorizationRule(
            rights=permissions
        )
        self._servicebus_client.topics.create_or_update_authorization_rule(self._resource_group, namespace_name, topic_name, token_name, parameters)
        
        # Reset keys if requested
        if reset_credentials:
            parameters = RegenerateAccessKeyParameters(key_type="PrimaryKey")
            self._servicebus_client.topics.regenerate_keys(self._resource_group, namespace_name, topic_name, token_name, parameters=parameters)
        
        # Generate SAS token        
        keys = self._servicebus_client.topics.list_keys(self._resource_group, namespace_name, topic_name, token_name)
        token = _generate_sas_token(namespace_name, entity_path, token_name, keys.primary_key)
        
        # Return token + needed info
        return {
            "auth_method": "cbs",
            "hostname": f"{namespace_name}.servicebus.windows.net",
            "port": "5671",
            "protocol": "amqps",
            "user": "",
            "password": "",
            "token": token,
            "entity": entity_path
        }

    def queue_spec_valid(self, namespace, name, spec, namespace_name):
        if not self.queue_exists(namespace, name, namespace_name):
            if self.topic_exists(namespace, name, namespace_name):
                return (False, "There is already a topic with the same name")
        queue_name = _calc_queue_name(namespace, name)
        if len(queue_name) > 260:
            return (False, f"calculated queue name '{queue_name}' is longer than 260 characters")
        for char in queue_name:
            if char not in ALLOWED_NAMESPACE_NAME_CHARACTERS:
                return (False, f"Character '{char}' is not allowed in name. Allowed are: letters, digits and hyphens")
        return True, ""

    def queue_exists(self, namespace, name, namespace_name):
        queue_name = _calc_queue_name(namespace, name)
        try:
            return self._servicebus_client.queues.get(self._resource_group, namespace_name, queue_name)
        except ResourceNotFoundError:
            return False

    def create_or_update_queue(self, namespace, name, spec, namespace_name):
        queue_name = _calc_queue_name(namespace, name)
        default_message_ttl = field_from_spec(spec, "queue.defaultTTLSeconds", _backend_config("queue.parameters.default_ttl_seconds", default=60*60*24*30))
        if default_message_ttl:
            default_message_ttl = timedelta(seconds=int(default_message_ttl))
        lock_duration = field_from_spec(spec, "queue.lockDurationSeconds", _backend_config("queue.parameters.lock_duration_seconds", default=60))
        if lock_duration:
            lock_duration = timedelta(seconds=int(lock_duration))
        parameters = SBQueue(
            default_message_time_to_live=default_message_ttl,
            max_size_in_megabytes=_backend_config("queue.parameters.max_size_in_megabytes", default=None),
            lock_duration=lock_duration,
            dead_lettering_on_message_expiration=field_from_spec(spec, "queue.enableDeadLettering", _backend_config("queue.parameters.dead_lettering_on_message_expiration", default=False)),
            max_delivery_count=int(field_from_spec(spec, "queue.maxDeliveryCount", _backend_config("queue.parameters.max_delivery_count", default=10))),
        )
        existing_queue = self.queue_exists(namespace, name, namespace_name)
        def diff():
            if existing_queue.default_message_time_to_live != parameters.default_message_time_to_live:
                return True
            if existing_queue.max_size_in_megabytes != parameters.max_size_in_megabytes:
                return True
            if existing_queue.lock_duration != parameters.lock_duration:
                return True
            if existing_queue.dead_lettering_on_message_expiration != parameters.dead_lettering_on_message_expiration:
                return True
            if existing_queue.max_delivery_count != parameters.max_delivery_count:
                return True
            return False
        if not existing_queue or diff():
            self._servicebus_client.queues.create_or_update(self._resource_group, namespace_name, queue_name, parameters)
        return queue_name

    def delete_queue(self, namespace, name, namespace_name):
        queue_name = _calc_queue_name(namespace, name)
        fake_delete = _backend_config("queue.fake_delete", default=False)
        if not fake_delete:
            self._servicebus_client.queues.delete(self._resource_group, namespace_name, queue_name)

    def create_or_update_queue_credentials(self, queue_name, namespace_name, reset_credentials=False):
        return self._create_or_update_queue_credentials(f"{queue_name}-owner", queue_name, namespace_name, [AccessRights.MANAGE, AccessRights.LISTEN, AccessRights.SEND], reset_credentials)

    def delete_queue_credentials(self, queue_name, namespace_name):
        try:
            self._servicebus_client.queues.delete_authorization_rule(self._resource_group, namespace_name, queue_name, f"{queue_name}-owner")
        except ResourceNotFoundError:
            pass

    def queue_consumer_spec_valid(self, namespace, name, spec):
        consumer_name = _calc_queue_consumer_name(namespace, name)
        if len(consumer_name) > 50:
            return (False, f"calculated consumer name '{consumer_name}' is longer than 50 characters")
        for char in consumer_name:
            if char not in ALLOWED_NAMESPACE_NAME_CHARACTERS:
                return (False, f"Character '{char}' is not allowed in name. Allowed are: letters, digits and hyphens")
        return (True, "")

    def create_or_update_queue_consumer_credentials(self, namespace, name, queue_name, namespace_name, reset_credentials=False):
        consumer_name = _calc_queue_consumer_name(namespace, name)
        return self._create_or_update_queue_credentials(consumer_name, queue_name, namespace_name, [AccessRights.LISTEN], reset_credentials)

    def delete_queue_consumer_credentials(self, namespace, name, queue_name, namespace_name):
        consumer_name = _calc_queue_consumer_name(namespace, name)
        try:
            self._servicebus_client.queues.delete_authorization_rule(self._resource_group, namespace_name, queue_name, consumer_name)
        except ResourceNotFoundError:
            pass

    def _create_or_update_queue_credentials(self, token_name, queue_name, namespace_name, permissions, reset_credentials=False):
        # Create or update authorization rule
        parameters = SBAuthorizationRule(
            rights=permissions
        )
        self._servicebus_client.queues.create_or_update_authorization_rule(self._resource_group, namespace_name, queue_name, token_name, parameters)
        
        # Reset keys if requested
        if reset_credentials:
            parameters = RegenerateAccessKeyParameters(key_type="PrimaryKey")
            self._servicebus_client.queues.regenerate_keys(self._resource_group, namespace_name, queue_name, token_name, parameters=parameters)
        
        # Generate SAS token        
        keys = self._servicebus_client.queues.list_keys(self._resource_group, namespace_name, queue_name, token_name)
        
        # Return token + needed info
        return {
            "auth_method": "user-password",
            "hostname": f"{namespace_name}.servicebus.windows.net",
            "port": "5671",
            "protocol": "amqps",
            "user": token_name,
            "password": keys.primary_key,
            "token": "",
            "entity": queue_name
        }


def _tags(namespace, name, extra_tags=None):
    tags = {f"{TAG_PREFIX}:namespace": namespace, f"{TAG_PREFIX}:name": name}
    for k, v in _backend_config("tags", default={}).items():
        tags[k] = v.format(namespace=namespace, name=name)
    if extra_tags:
        for k, v in extra_tags.items():
            tags[f"{TAG_PREFIX}:{k}"] = v
    return tags


def _generate_sas_token(servicebus_name, entity_path, authorization_rule_name, key):
    uri = urllib.parse.quote_plus(f"https://{servicebus_name}.servicebus.windows.net/{entity_path}")
    sas = key.encode('utf-8')
    expiry = str(int(time.time() + 60*60*24*365*10)) # 10 years, we handle expiry via the authorization rules
    string_to_sign = (uri + '\n' + expiry).encode('utf-8')
    signed_hmac_sha256 = hmac.HMAC(sas, string_to_sign, hashlib.sha256)
    signature = urllib.parse.quote(base64.b64encode(signed_hmac_sha256.digest()))
    return  f'SharedAccessSignature sr={uri}&sig={signature}&se={expiry}&skn={authorization_rule_name}'
