import os
from hybridcloud_core.configuration import config_get
import requests
from ..util import helm
from ..util.constants import HELM_BASE_PATH


def _backend_config(key, default=None, fail_if_missing=False):
    return config_get(f"backends.rabbitmq.{key}", default=default, fail_if_missing=fail_if_missing)


def _calc_helm_release_name(namespace, name):
    return f"rabbitmq-{name}"


def _calc_topic_name(namespace, name):
   return f"{namespace}-{name}"


def _calc_queue_name(namespace, name):
    return f"{namespace}-{name}"


def _calc_subscription_name(namespace, name):
    return f"{namespace}-{name}"


class RabbitMQException(Exception):
    pass


class RabbitMQBackend:
    def __init__(self, logger):
        self._logger = logger
        self._admin_auth = ("admin", "admin")

    def _api_get(self, broker, url, json=None):
        return requests.get(f"http://{broker}.svc.cluster.local:15672/api/{url}", json=json, auth=self._admin_auth)
    
    def _api_post(self, broker, url, json=None):
        response = requests.post(f"http://{broker}.svc.cluster.local:15672/api/{url}", json=json, auth=self._admin_auth)
        if not response.ok:
            raise RabbitMQException(f"Failed to execute operation: {response.status_code}: {response.text}")
        return response

    def _api_put(self, broker, url, json=None):
        response = requests.put(f"http://{broker}.svc.cluster.local:15672/api/{url}", json=json, auth=self._admin_auth)
        if not response.ok:
            raise RabbitMQException(f"Failed to execute operation: {response.status_code}: {response.text}")
        return response

    def _api_delete(self, broker, url):
        return requests.delete(f"http://{broker}.svc.cluster.local:15672/api/{url}", auth=self._admin_auth)       
        
    def broker_spec_valid(self, namespace, name, spec):
        broker_name = _calc_helm_release_name(namespace, name)
        if len(broker_name) > 63:
            return (False, f"calculated namespace name '{broker_name}' is too long")
        return (True, "")

    def broker_exists(self, namespace, name):
        return helm.check_installed(namespace, f"rabbitmq-{name}")

    def create_or_update_broker(self, namespace, name, spec, extra_tags=None):
        helm_release = _calc_helm_release_name(namespace, name)
        values = f"""
fullnameOverride: {helm_release}
auth:
  username: admin
  password: admin
extraPlugins: "rabbitmq_amqp1_0"
clustering:
  enabled: false
        """
        helm.install_upgrade(namespace, helm_release, os.path.join(HELM_BASE_PATH, "rabbitmq"), "--wait", values=values)
        return f"{helm_release}.{namespace}"

    def delete_broker(self, namespace, name):
        helm_release = _calc_helm_release_name(namespace, name)
        helm.uninstall(namespace, helm_release)

    def topic_spec_valid(self, namespace, name, spec, broker_name):
        if not self.topic_exists(namespace, name, broker_name):
            if self.queue_exists(namespace, name, broker_name):
                return (False, "There is already a queue with the same name")
        return True, ""

    def topic_exists(self, namespace, name, broker_name):
        topic_name = _calc_topic_name(namespace, name)
        response = self._api_get(broker_name, f"exchanges/%2F/{topic_name}")
        return response.ok

    def create_or_update_topic(self, namespace, name, spec, broker_name):
        topic_name = _calc_topic_name(namespace, name)
        self._api_put(broker_name, f"exchanges/%2F/{topic_name}", json={"type":"fanout","auto_delete":False,"durable":True,"internal":False,"arguments":{}})
        return topic_name

    def delete_topic(self, namespace, name, broker_name):
        topic_name = _calc_topic_name(namespace, name)
        self._api_delete(broker_name, f"exchanges/%2F/{topic_name}")

    def create_or_update_topic_credentials(self, topic_name, broker_name, reset_credentials=False):
        username = f"{topic_name}-owner"
        password = self._create_or_update_user(username, broker_name, reset_credentials)
        return {
            "auth_method": "user-password",
            "hostname": f"{broker_name}.svc.cluster.local",
            "port": "5672",
            "protocol": "amqp",
            "user": username,
            "password": password,
            "token": "",
            "entity": f"/exchange/{topic_name}"
        }

    def delete_topic_credentials(self, topic_name, broker_name):
        username = f"{topic_name}-owner"
        self._delete_user(username, broker_name)

    def topic_subscription_spec_valid(self, namespace, name, spec):
        return (True, "")

    def topic_subscription_exists(self, namespace, name, topic_name, broker_name):
        subscription_name = _calc_subscription_name(namespace, name)
        response = self._api_get(broker_name, f"queues/%2F/{subscription_name}")
        return response.ok

    def create_or_update_topic_subscription(self, namespace, name, spec, topic_name, broker_name):
        subscription_name = _calc_subscription_name(namespace, name)
        self._api_put(broker_name, f"queues/%2F/{subscription_name}", json={"auto_delete":False,"durable":False,"arguments":{}})
        self._api_post(broker_name, f"bindings/%2F/e/{topic_name}/q/{subscription_name}", json={})
        return subscription_name

    def delete_topic_subscription(self, namespace, name, topic_name, broker_name):
        subscription_name = _calc_subscription_name(namespace, name)
        self._api_delete(broker_name, f"bindings/%2F/e/{topic_name}/q/{subscription_name}/~")
        self._api_delete(broker_name, f"queues/%2F/{subscription_name}")

    def create_or_update_topic_subscription_credentials(self, subscription_name, topic_name, broker_name, reset_credentials=False):
        username = f"subscription-{subscription_name}"
        password = self._create_or_update_user(username, broker_name, reset_credentials)
        return {
            "auth_method": "user-password",
            "hostname": f"{broker_name}.svc.cluster.local",
            "port": "5672",
            "protocol": "amqp",
            "user": username,
            "password": password,
            "token": "",
            "entity": f"/queue/{subscription_name}"
        }

    def delete_topic_subscription_credentials(self, subscription_name, topic_name, broker_name):
        username = f"subscription-{subscription_name}"
        self._delete_user(username, broker_name)

    def queue_spec_valid(self, namespace, name, spec, broker_name):
        if not self.queue_exists(namespace, name, broker_name):
            if self.topic_exists(namespace, name, broker_name):
                return (False, "There is already a topic with the same name")
        return True, ""

    def queue_exists(self, namespace, name, broker_name):
        queue_name = _calc_queue_name(namespace, name)
        response = self._api_get(broker_name, f"queues/%2F/{queue_name}")
        return response.ok

    def create_or_update_queue(self, namespace, name, spec, broker_name):
        queue_name = _calc_queue_name(namespace, name)
        self._api_put(broker_name, f"queues/%2F/{queue_name}", json={"auto_delete":False,"durable":False,"arguments":{}})
        return queue_name

    def delete_queue(self, namespace, name, broker_name):
        queue_name = _calc_queue_name(namespace, name)
        self._api_delete(broker_name, f"queues/%2F/{queue_name}")

    def create_or_update_queue_credentials(self, queue_name, broker_name, reset_credentials=False):
        username = f"{queue_name}-owner"
        password = self._create_or_update_user(username, broker_name, reset_credentials)
        return {
            "auth_method": "user-password",
            "hostname": f"{broker_name}.svc.cluster.local",
            "port": "5672",
            "protocol": "amqp",
            "user": username,
            "password": password,
            "token": "",
            "entity": f"/queue/{queue_name}"
        }

    def delete_queue_credentials(self, queue_name, broker_name):
        username = f"{queue_name}-owner"
        self._delete_user(username, broker_name)

    def queue_consumer_spec_valid(self, namespace, name, spec):
        return (True, "")

    def create_or_update_queue_consumer_credentials(self, namespace, name, queue_name, broker_name, reset_credentials=False):
        username = f"consumer-{namespace}-{name}"
        password = self._create_or_update_user(username, broker_name, reset_credentials)
        return {
            "auth_method": "user-password",
            "hostname": f"{broker_name}.svc.cluster.local",
            "port": "5672",
            "protocol": "amqp",
            "user": username,
            "password": password,
            "token": "",
            "entity": f"/queue/{queue_name}"
        }

    def delete_queue_consumer_credentials(self, namespace, name, queue_name, broker_name):
        username = f"consumer-{namespace}-{name}"
        self._delete_user(username, broker_name)

    def _create_or_update_user(self, username, broker_name, reset_credentials=False):
        response = self._api_get(broker_name, f"users/{username}")
        password = username
        if not response.ok or reset_credentials:
            self._api_put(broker_name, f"users/{username}", json={"password":password,"tags":"administrator"})
        self._api_put(broker_name, f"permissions/%2F/{username}", json={"configure":".*","write":".*","read":".*"})
        return password

    def _delete_user(self, username, broker_name):
        self._api_delete(broker_name, f"users/{username}")
