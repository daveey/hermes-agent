"""Initialize per-request provider and offline retry budgets."""


def apply_retry_config(agent, section):
    try:
        agent._api_max_retries = max(int(section.get("api_max_retries", 3)), 1)
    except (TypeError, ValueError):
        agent._api_max_retries = 3
    try:
        agent._auto_recovery_cycles = max(int(section.get("auto_recovery_cycles", 5)), 0)
    except (TypeError, ValueError):
        agent._auto_recovery_cycles = 5
    try:
        agent._offline_wait_seconds = max(float(section.get("offline_wait_seconds", 900)), 0.0)
    except (TypeError, ValueError):
        agent._offline_wait_seconds = 900.0
