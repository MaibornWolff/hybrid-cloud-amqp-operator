# Hybrid-cloud AMQP Operator

The hybrid-cloud-amqp-operator is designed for hybrid-cloud, multi-teams kubernetes platforms to allow teams to deploy and manage their own AMQP message brokers via kubernetes without cloud provider specific provisioning.

In classical cloud environments things like message brokers would typically be managed by a central platform team via infrastructure automation like terraform. But this means when different teams are active on such a platform there exists a bottleneck because that central platform team must handle all requests for access to the broker. With this operator teams in kubernetes gain the potential to manage the broker on their own. And because the operator integrates into the kubernetes API the teams have the same unified interface/API for all their deployments: Kubernetes YAMLs.

Additionally the operator also provides a consistent interface regardless of the environment (cloud provider, on-premise) the kubernetes cluster runs in. This means in usecases where teams have to deploy to clusters running in different environments they still get the same interface on all clusters and do not have to concern themselves with any differences.

Main features:

* Provides Kubernetes Custom resources for deploying and managing AMQP Topics, Queues and read access to both
* Abstracted, unified API regardless of target environment (cloud, on-premise)
* Currently supported backends:
  * Azure Service Bus
  * [RabbitMQ](https://www.rabbitmq.com/) (proof-of-concept, not for production environments)

This operator was initially developed by MaibornWolff for and with [Weidmueller](https://www.weidmueller.com/) and is used by Weidmueller as part of their cloud solutions.

## Quickstart

To test out the operator you do not need Azure, you just need a kubernetes cluster (you can for example create a local one with [k3d](https://k3d.io/)) and cluster-admin rights on it.

1. Run `helm repo add maibornwolff https://maibornwolff.github.io/hybrid-cloud-amqp-operator/` to prepare the helm repository.
2. Run `helm install hybrid-cloud-amqp-operator-crds maibornwolff/hybrid-cloud-amqp-operator-crds` and `helm install hybrid-cloud-amqp-operator maibornwolff/hybrid-cloud-amqp-operator` to install the operator.
3. Check if the pod of the operator is running and healthy: `kubectl get pods -l app.kubernetes.io/name=hybrid-cloud-amqp-operator`.
4. Create your first broker: `kubectl apply -f examples/broker.yaml`. By default the operator will now deploy a rabbitmq instance inside the cluster.
5. Wait for the broker to start (this can take a minute or two): `kubectl wait --for=condition=ready --timeout=120s pods/rabbitmq-demo-0`.
6. Create your first queue: `kubectl apply -f examples/queue.yaml`.
7. Retrieve the credentials for the queue: `kubectl get secret somequeue-credentials -o yaml`. You can now connect to the broker and send messages to the queue.
8. After you are finished, delete everything: `kubectl delete -f examples/queue.yaml -f examples/namespace.yaml`.

## Operations Guide

To achieve its hybrid-cloud feature the operator abstracts between the generic API and the concrete implementation for a specific cloud service. The concrete implementations are called backends. You can configure which backends should be active in the configuration. If you have several backends active the user can also select one.

The operator can be configured using a yaml-based config file. This is the complete configuration file with all options. Please refer to the comments in each line for explanations:

```yaml
handler_on_resume: false  # If set to true the operator will reconcile every available resource on restart even if there were no changes
backend: azureservicebus  # Default backend to use, required, allowed: azureservicebus, rabbitmq
allowed_backends: []  # List of backends the users can select from. If list is empty the default backend is always used regardless of if the user selects a backend 
backends:  # Configuration for the different backends. Required fields are only required if the backend is used
  azureservicebus:  # Configuration for the azure service bus backend
    subscription_id: 1-2-3-4-5  # Azure Subscription id to provision database in, required
    location: westeurope  # Location to provision database in, required
    resource_group: foobar-rg  # Resource group to provision database in, required
    sku: Standard  # SKU to use for the ServiceBus namespaces. Allowed: Basic, Standard, Premium
    capacity:  # 
    name_pattern_namespace: "{namespace}-{name}"  # Name pattern to use for the ServiceBus namespaces
    fake_delete: false  # If set to true the operator will not actually delete the servicebus namespace when the object in kubernetes is deleted, protects against accidental deletions
    topic:  # Options in regards to Topics
      fake_delete: false  # If set to true the operator will not actually delete the topic when the object in kubernetes is deleted
      name_pattern: "{namespace}-{name}"  # Name pattern to use for the ServiceBus topic
      name_pattern_subscription: "{namespace}-{name}"  # Name pattern to use for the subscription
      parameters:
        default_ttl_seconds: 31536000  # Default TTL for messages in seconds if not set in the message, defaults to 1 year, can be overwritten per topic in the custom object
        max_size_in_megabytes: 1024  # Size of the topic in MB, default is 1024
        support_ordering: true  # Value that indicates whether the topic supports ordering.
    subscription:  # Options in regards to TopicSubscriptions
      parameters:
        default_ttl_seconds: 31536000  # Default TTL for messages in seconds if not set in the message, defaults to 1 year, can be overwritten per subscription in the custom object
        lock_duration_seconds: 60 # Lock time duration for the subscription in seconds, default is 1 minute, maximum is 5 minutes, can be overwritten per subscription in the custom object
        dead_lettering_on_message_expiration: false  # If set to true expired messages will be sent to a special dead letter queue, can be overwritten per subscription in the custom object
        max_delivery_count: 10  # Number of maximum deliveries, can be overwritten per subscription in the custom object
    queue:
      fake_delete: false  # If set to true the operator will not actually delete the queue when the object in kubernetes is deleted
      name_pattern: "{namespace}-{name}"  # Name pattern to use for the ServiceBus queue
      name_pattern_consumer: "{namespace}-{name}"  # Name pattern to use for the authorization rule created for each consumer
      parameters:
        default_ttl_seconds: 31536000  # Default TTL for messages in seconds if not set in the message, defaults to 1 year, can be overwritten per queue in the custom object
        max_size_in_megabytes: 1024  # Size of the queue in MB, default is 1024
        lock_duration_seconds: 60 # Lock time duration for the subscription in seconds, default is 1 minute, maximum is 5 minutes, can be overwritten per queue in the custom object
        dead_lettering_on_message_expiration: false  # If set to true expired messages will be sent to a special dead letter queue, can be overwritten per queue in the custom object
        max_delivery_count: 10  # Number of maximum deliveries, can be overwritten per queue in the custom object
cross_namespace:
  allow_produce: false # If set to true, topics/queues can be associated with an AMQPBroker from a different K8s namespace
  allow_consume: false # If set to true, TopicSubscribers and QueueConsumers can be created for a queue/topic from a different K8s namespace
```

Single configuration options can also be provided via environment variables, the complete path is concatenated using underscores, written in uppercase and prefixed with `HYBRIDCLOUD_`. As an example: `backends.azure.subscription_id` becomes `HYBRIDCLOUD_BACKENDS_AZURE_SUBSCRIPTION_ID`.

To protect Namespaces, Topics and Queues against accidential deletion you can enable `fake_delete` in the backends. If this is enabled the operator will not acutally delete the resource when the kubernetes object is deleted. This can be used in situations where the operator is freshly introduced in an environment where the users have little experience with this type of declarative management and you want to reduce the risk of accidental data loss.

For the operator to interact with Azure it needs credentials. For local testing it can pick up the token from the azure cli but for real deployments it needs a dedicated service principal. Supply the credentials for the service principal using the environment variables `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` (if you deploy via the helm chart use the use `envSecret` value). Depending on the backend the operator requires the following azure permissions within the scope of the resource group it deploys to:

* `Microsoft.ServiceBus/*` (or assign the role `Azure Service Bus Data Owner`)

The RabbitMQ backend is only a proof-of-concept that is intended for testing and demo purposes. In particular authentication is only implemented as a dummy and is not secure.

### Deployment

The operator can be deployed via helm chart:

1. Run `helm repo add maibornwolff https://maibornwolff.github.io/hybrid-cloud-amqp-operator/` to prepare the helm repository.
2. Run `helm install hybrid-cloud-amqp-operator-crds maibornwolff/hybrid-cloud-amqp-operator-crds` to install the CRDs for the operator.
3. Run `helm install hybrid-cloud-amqp-operator maibornwolff/hybrid-cloud-amqp-operator -f values.yaml` to install the operator.

Configuration of the operator is done via helm values. For a full list of the available values see the [values.yaml in the chart](./helm/hybrid-cloud-amqp-operator/values.yaml). These are the important ones:

* `operatorConfig`: overwrite this with your specific operator config
* `envSecret`: Name of a secret with sensitive credentials (e.g. Azure service principal credentials)
* `serviceAccount.create`: Either set this to true or create the serviceaccount with appropriate permissions yourself and set `serviceAccount.name` to its name

## User Guide

The operator is completely controlled via Kubernetes custom resources. Once a server object is created the operator will utilize one of its backends to provision the actual resources. The following custom resources are available:

* `AMQPBroker`: The top-level object, depending on the configuration the operator might deploy a separate AMQP broker for each namespace
* `AMQPTopic`: Represents a Topic in an AMQP broker, must be associated with an `AMQPBroker`
* `AMQPTopicSubscription`: Represents a subscription for a topic, must be associated with an `AMQPTopic`
* `AMQPQueue`: Represents a Queue in an AMQP broker, must be associated with an `AMQPBroker`
* `AMQPQueueConsumer`: Represents a read-only access to a queue, must be associated with an `AMQPQueue`

The `AMQPBroker` has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: AMQPBroker
metadata:
  name: mybroker  # Name, will be used as the name of the broker instance (or in Azure as the name of the ServiceBus namespace)
  namespace: default
spec:
  backend: # Optional, backend to use, only relevant if the operator configuration allows severval, normally not needed as the cluster admin will preconfigure the best default
  allowedK8sNamespaces: [] # Optional, list of kubernetes namespaces that can create topics and queues in this broker, only relevant if the admin allows cross-namespace usage, if empty or omitted only own namespace is allowed
```

The `AMQPTopic` has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: AMQPTopic
metadata:
  name: foobar  # Name of the topic, will be used to construct the topic name in the broker
  namespace: default
spec:
  brokerRef:  # References the AMQPBroker
    name: mybroker  # Required, Name of the AMQP namespace
    namespace: default  # Optional, kubernetes namespace of the AMQ namespace, only required if AMQP namespace is in a different kubernetes namespace
  topic:
    defaultTTLSeconds: # Optional, default TTL in seconds for messages that do not have a TTL set
  credentialsSecret: foobar-topic-creds  # Name of a secret where credentials to access the topic will be stored by the operator
  allowedK8sNamespaces: [] # Optional, list of kubernetes namespaces that can create subscriptions for this topic, only relevant if the admin allows cross-namespace usage, if empty or omitted only own namespace is allowed
```

The `AMQPTopicSubscription` has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: AMQPTopicSubscription
metadata:
  name: foobar-reader  # Name of the subscription, will be used to construct the subscription name in the broker
  namespace: default
spec:
  topicRef:  # References the AMQPTopic
    name: foobar  # Required, Name of the AMQP topic
    namespace: default  # Optional, kubernetes namespace of the AMQ topic, only required if AMQP topic is in a different kubernetes namespace
  subscription:
    defaultTTLSeconds:  # Optional, default TTL in seconds for messages that do not have a TTL set
    lockDurationSeconds: 60 # Optional, the lock duration in seconds
    enableDeadLettering: false # If set to true expired messages will be sent to a special dead letter queue
    maxDeliveryCount: 10  # Number of deliveries to attempt 
  credentialsSecret: foobar-reader-creds  # Name of a secret where credentials to access the topic will be stored by the operator
```

The `AMQPQueue` has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: AMQPQueue
metadata:
  name: foobar  # Name of the queue, will be used to construct the queue name in the broker
  namespace: default
spec:
  brokerRef:  # References the AMQPBroker
    name: mybroker  # Required, Name of the AMQP namespace
    namespace: default  # Optional, kubernetes namespace of the AMQ namespace, only required if AMQP namespace is in a different kubernetes namespace
  queue:
    defaultTTLSeconds:  # Optional, default TTL in seconds for messages that do not have a TTL set
    lockDurationSeconds: 60 # Optional, the lock duration in seconds
    enableDeadLettering: false # If set to true expired messages will be sent to a special dead letter queue
    maxDeliveryCount: 10  # Number of deliveries to attempt 
  credentialsSecret: foobar-queue-creds  # Name of a secret where credentials to access the queue will be stored by the operator
  allowedK8sNamespaces: [] # Optional, list of kubernetes namespaces that can create consumers for this queue, only relevant if the admin allows cross-namespace usage, if empty or omitted only own namespace is allowed
```

The `AMQPQueueConsumer` has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: AMQPQueueConsumer
metadata:
  name: foobar-consumer  # Name of the consumer, will be used to construct the consumer name in the broker
  namespace: default
spec:
  queueRef:  # References the AMQPQueue
    name: mybroker  # Required, Name of the AMQP queue
    namespace: default  # Optional, kubernetes namespace of the AMQ queue, only required if AMQP queue is in a different kubernetes namespace
  credentialsSecret: foobar-consumer-creds  # Name of a secret where credentials to access the queue will be stored by the operator
```

To connect to an AMQP broker provisioned by the operator you need a library that speaks the AMQP 1.0 protocol (many libraries only speak AMQP 0.9 which is not compatible). The credentials secret created by the operator contains the following fields which you need to connect to the broker:

* `hostname`: The hostname to connect to
* `port`: The port to use (will normally either be 5671 or 5672)
* `protocol`: The protocol to use, is either `amqp` for unencrypted connection (with possible TLS switch after connect) or `amqps` for TLS-encrypted connections
* `auth_method`: The method to authenticate against the broker, is either `user-password` or `cbs` (see below)
* `user`: If `auth_method` is `user-password` contains the username to use
* `password`: If `auth_method` is `user-password` contains the password to use
* `token`:  If `auth_method` is `cbs` contains the token to use
* `entity`: The address/path of the entity (topic, subscription, queue)

Depending on the backend and the requested object the operator provides credentials for one of two authentication mechanisms which a client using an operator-provisioned topic/queue should both support. One is `user-password` which is a classic username/password authentication. The other is `cbs` which is an extension to the AMQP protocol spec, it is described in the [Azure ServiceBus documentation](https://docs.microsoft.com/en-us/azure/service-bus-messaging/service-bus-sas#use-the-shared-access-signature-at-amqp-level).

## Development

The operator is implemented in Python using the [Kopf](https://github.com/nolar/kopf) ([docs](https://kopf.readthedocs.io/en/stable/)) framework.

To run it locally follow these steps:

1. Create and activate a local python virtualenv
2. Install dependencies: `pip install -r requirements.txt`
3. Setup a local kubernetes cluster, e.g. with k3d: `k3d cluster create`
4. Apply the CRDs in your local cluster: `kubectl apply -f helm/hybrid-cloud-amqp-operator-crds/templates/`
5. If you want to deploy to azure: Either have the azure cli installed and configured with an active login or export the following environment variables: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`
6. Adapt the `config.yaml` to suit your needs
7. Run `kopf run main.py -A`
8. In another window apply some objects to the cluster to trigger the operator (see the `examples` folder)

The code is structured in the following packages:

* `handlers`: Implements the operator interface for the provided custom resources, reacts to create/update/delete events in handler functions
* `backends`: Backends for the different environments
* `util`: Helper and utility functions

To locally test the helm backends the operator needs a way to communicate with rabbitmq running in the cluster. You can use [sshuttle](https://github.com/sshuttle/sshuttle) and [kuttle](https://github.com/kayrus/kuttle) for that. Run:

```bash
kubectl run kuttle --image=python:3.10-alpine --restart=Never -- sh -c 'exec tail -f /dev/null'
sshuttle --dns -r kuttle -e kuttle <internal-ip-range-of-your-cluster>
```

### Tips and tricks

* Kopf marks every object it manages with a finalizer, that means that if the operator is down or doesn't work a `kubectl delete` will hang. To work around that edit the object in question (`kubectl edit <type> <name>`) and remove the finalizer from the metadata. After that you can normally delete the object. Note that in this case the operator will not take care of cleaning up any azure resources.
* If the operator encounters an exception while processing an event in a handler, the handler will be retried after a short back-off time. During the development you can then stop the operator, make changes to the code and start the operator again. Kopf will pick up again and rerun the failed handler.
* When a handler was successfull but you still want to rerun it you need to fake a change in the object being handled. The easiest is adding or changing a label.
