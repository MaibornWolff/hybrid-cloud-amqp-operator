from typing import List
from prometheus_client import Counter

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

class ACTIONS:
    RESUME = "resume"
    CREATE_OR_UPDATE = "create_or_update"
    DELETE = "delete"

    def list():
        return [ACTIONS.RESUME, ACTIONS.CREATE_OR_UPDATE, ACTIONS.DELETE]


def initialize_prometheus_metrics(handler_name: str, actions: List[str] = ACTIONS.list()) -> None:
    for action in actions:
        PROMETHEUS_HANDLER_CALLS_TOTAL_COUNTER.labels(handler=handler_name, action=action)
        PROMETHEUS_HANDLER_EXCEPTION_COUNTER.labels(handler=handler_name, action=action)
