"""Small rendering phases for the CLI's full-card view."""


def print_model(task, field):
    if task.model_override:
        provider = f" (provider: {task.provider_override})" if task.provider_override else ""
        field("model", f"{task.model_override}{provider}")
