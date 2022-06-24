import kubernetes
from hybridcloud_core.k8s.resources import Resource, Scope


API_GROUP = "hybridcloud.maibornwolff.de"


AMQPBroker = Resource(API_GROUP, "v1alpha1", "amqpbrokers", "AMQPBroker", Scope.NAMESPACED)
AMQPTopic = Resource(API_GROUP, "v1alpha1", "amqptopics", "AMQPTopic", Scope.NAMESPACED)
AMQPQueue = Resource(API_GROUP, "v1alpha1", "amqpqueues", "AMQPQueue", Scope.NAMESPACED)
AMQPTopicSubscription = Resource(API_GROUP, "v1alpha1", "amqptopicsubscriptions", "AMQPTopicSubscription", Scope.NAMESPACED)
AMQPQueueConsumer = Resource(API_GROUP, "v1alpha1", "amqpqueueconsumers", "AMQPQueueConsumer", Scope.NAMESPACED)
