from typing import List
from prometheus_client import Counter, Gauge
from . import k8s
import kopf

PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER = Counter(
    'handler_calls_total', 
    'Amount of times a handler has been called', 
    ["handler", "action"]
)

PROMETHEUS_HANDLER_EXCEPTION_COUNTER = Counter(
    'handler_exceptions_total', 
    'Amount of exceptions thrown in kopf handlers', 
    ["handler", "action"]
)

PROMETHEUS_RESOURCES_CREATED_TOTAL_GAUGE = Gauge(
    'resources_created_total', 
    'Amount of resources created using the operator', 
    ["type"]
)

class ACTIONS:
    RESUME = "resume"
    CREATE_OR_UPDATE = "create_or_update"
    DELETE = "delete"

    def list():
        return [ACTIONS.RESUME, ACTIONS.CREATE_OR_UPDATE, ACTIONS.DELETE]


@kopf.index(*k8s.AMQPBroker.kopf_on())
@kopf.index(*k8s.AMQPQueueConsumer.kopf_on())
@kopf.index(*k8s.AMQPQueue.kopf_on())
@kopf.index(*k8s.AMQPTopicSubscription.kopf_on())
@kopf.index(*k8s.AMQPTopic.kopf_on())
def resource_index(namespace, body, **_):
    return { namespace: body.get("kind") }

def initialize_prometheus_handler_metrics(handler_name: str, actions: List[str] = ACTIONS.list()) -> None:
    for action in actions:
        PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=handler_name, action=action)
        PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=handler_name, action=action)

def initialize_prometheus_resource_gauge(type: str) -> None:
    PROMETHEUS_RESOURCES_CREATED_TOTAL_GAUGE.labels(type=type)

def extract_count_from_kopf_index(index: kopf.Index, resource_type: str) -> int:
    count = 0
    resources = [] 
    for value in index.values():
        resources += value
    
    for resource in resources:
        if resource == resource_type:
            count += 1

    return count