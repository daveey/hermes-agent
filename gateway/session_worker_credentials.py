"""Private, execution-bound credential handles; never part of the bootstrap pipe."""
import copy
import hmac
import json

from hermes_state_runtime import RuntimeStoreError

# Positive exec allowlist, not a credential-name denylist. In particular no arbitrary
# HERMES_*, PYTHON*, shell startup, cloud, or profile .env inputs are inherited.
EXEC_ENV = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ',
    'TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
    'HERMES_REAL_HOME', 'HERMES_MANAGED_HOME', 'HERMES_NEMO_RELAY_PLUGINS_TOML',
    'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'no_proxy',
    'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS',
    'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH',
    'SSH_AUTH_SOCK', 'DISPLAY', 'XDG_CONFIG_HOME', 'XDG_DATA_DIRS', 'LD_LIBRARY_PATH',
    'TERM', 'KUBECONFIG', 'HERMES_BUNDLED_SKILLS',
})


def _profile_values(authority, policy):
    from pathlib import Path
    from agent.secret_scope import build_profile_secret_scope
    from tools.environments.local import _is_routed_home
    from tui_gateway.launch_profile_policy import launch_secret_scope
    home = Path(str(authority.profile_id))
    if not home.is_absolute():
        return {}
    values = {} if _is_routed_home(home) else launch_secret_scope(home)
    if not policy.ignore_user_config:
        values = {**values, **build_profile_secret_scope(home)}
    return values


def _route_names(route, config, values):
    from hermes_cli import auth
    from hermes_cli.runtime_provider import _config_base_url_trustworthy_for_bare_custom
    from hermes_cli.runtime_provider_custom import (
        custom_provider_config_path_for_url, named_custom_provider_config_path)
    from gateway.session_policy_credentials import _defined
    provider = str(route.get('provider') or 'auto').strip().lower()
    provider = auth._plugin_aliases().get(provider, provider)
    entry_path = (custom_provider_config_path_for_url(route.get('base_url'), config)
                  if provider == 'custom' else named_custom_provider_config_path(provider, config))
    entry = _defined(config, entry_path) if entry_path else {}
    names = {block[key] for block in (route, entry) if isinstance(block, dict)
             for key in ('key_env', 'api_key_env') if isinstance(block.get(key), str) and block[key]}
    if provider in ('', 'auto'):
        if _config_base_url_trustworthy_for_bare_custom(route.get('base_url'), provider):
            provider = 'custom'
        elif auth._openrouter_auto_detected(lambda name: values.get(name, '')):
            provider = 'openrouter'
        else:
            provider = auth._env_key_auto_detected(lambda name: values.get(name, ''), None)
    if provider == 'openrouter':
        if auth.has_usable_secret(str(values.get('OPENROUTER_API_KEY') or '')):
            names.add('OPENROUTER_API_KEY')
        elif str(values.get('OPENAI_API_KEY') or '').startswith('sk-or-'):
            names.add('OPENAI_API_KEY')
    elif provider == 'custom' and not names:
        names.add('OPENAI_API_KEY')
    registered = auth.PROVIDER_REGISTRY.get(provider) if provider else None
    if registered:
        names.update(registered.api_key_env_vars)
    if provider == 'bedrock':
        names.update({'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                      'AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_PROFILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE'})
    return names


def route_environment(authority, policy, config):
    """Only names selected by declared inference routes, never all profile secrets."""
    values = _profile_values(authority, policy)
    main = config.get('model') if isinstance(config.get('model'), dict) else {}
    routes = [main]
    for key in ('fallback_model', 'fallback_providers'):
        value = config.get(key, [])
        routes.extend(value if isinstance(value, list) else [value])
    auxiliary = config.get('auxiliary')
    if isinstance(auxiliary, dict):
        for route in auxiliary.values():
            if isinstance(route, dict):
                routes.append({**main, **route} if not route.get('provider') and not route.get('base_url') else route)
                routes.extend(route.get('fallback_chain') or [])
    names = set()
    for route in routes:
        if isinstance(route, dict):
            names.update(_route_names(route, config, values))
    return {name: values[name] for name in names if values.get(name) is not None}


def stage_credentials(authority, scope, policy, config, terminal, request):
    """Keep material on the owner; the exact adopted execution is its only handle."""
    from gateway.session_policy import _extract_config_secrets, route_launch_key
    private = {}
    config, terminal, request = copy.deepcopy(config), copy.deepcopy(terminal), copy.deepcopy(request)
    route_config = dict(config)
    construct = request.get('construct_v1')
    if isinstance(construct, dict) and 'fallback_model' in construct:
        route_config['fallback_model'] = construct['fallback_model']
    environment = route_environment(authority, policy, route_config)
    environment.update(tool_environment(authority, policy, config))
    key = route_launch_key(authority, policy, config)
    _extract_config_secrets(config, private)
    _extract_config_secrets(terminal, private, (None,))
    _extract_config_secrets(request, private, ('request_v1',))
    # No credential RPC is necessary for credential-free policies/fixtures.
    if not private and key is None and not environment:
        return config, terminal, request, False
    slots = getattr(authority, '_managed_credentials', None)
    if slots is None:
        slots = authority._managed_credentials = {}
    slots[scope['execution_id']] = (dict(scope), {'api_key': key, 'environment': environment,
        'entries': [[list(path), value] for path, value in private.items()]})
    return config, terminal, request, True


def release_credentials(authority, scope):
    getattr(authority, '_managed_credentials', {}).pop((scope or {}).get('execution_id'), None)


def resolve_credentials(authority, params):
    """Called only after worker_request verified a live adopted producer and epoch."""
    slot = getattr(authority, '_managed_credentials', {}).get(params['execution_id'])
    if slot is None:
        raise RuntimeStoreError('launch_credentials_unavailable')
    scope, material = slot
    if (any(params.get(key) != scope[key] for key in scope if key != 'secret')
            or not hmac.compare_digest(params['secret'], scope['secret'])):
        raise RuntimeStoreError('permission_denied')
    worker = getattr(authority, '_managed_workers', {}).get(scope['session_id'])
    if worker is not None:
        worker.secrets.extend(v for v in [material['api_key'], *(v for _, v in material['entries'])]
                              if isinstance(v, str) and v)
    return copy.deepcopy(material)


def hydrate_credentials(frame, rpc):
    """Resolve the handle via the authenticated owner, before freezing child config."""
    bind_completion_owner(frame)
    request = json.loads(frame['policy']['request_json'])
    if not request.pop('credentials_v1', False):
        return frame
    import os
    material = rpc('worker.credentials', **frame['scope'])
    os.environ.update(material['environment'])
    config = json.loads(frame['policy']['config_json'])
    terminal = json.loads(frame['policy']['terminal_json'])
    for path, value in material['entries']:
        roots = {None: terminal, 'request_v1': request}
        target = roots.get(path[0], config)
        parts = path[1:] if path[0] in roots else path
        for key in parts[:-1]:
            target = target[key]
        target[parts[-1]] = value
    return {**frame, 'api_key': material['api_key'], 'policy': {**frame['policy'],
        'config_json': json.dumps(config), 'terminal_json': json.dumps(terminal),
        'request_json': json.dumps(request)}}


def tool_environment(authority, policy, config):
    """Only credential requirements of tools selected by the child's frozen policy."""
    from tools.registry import registry
    from toolsets import resolve_toolset
    from tools.environments.local_env_policy import _is_hermes_internal_secret
    selected = {name for toolset in policy.current_toolsets(config) for name in resolve_toolset(toolset)}
    names = {name for entry in registry.get_all_entries() if entry.name in selected
             for name in entry.requires_env if isinstance(name, str) and not _is_hermes_internal_secret(name)}
    values = _profile_values(authority, policy)
    return {name: values[name] for name in names if values.get(name) is not None}


def bind_completion_owner(frame):
    """A nonsecret completion destination, not Desktop backend authentication."""
    from agent import runtime_session_store
    request = json.loads(frame['policy']['request_json'])
    runtime_session_store._desktop_completion_owner = request.get('desktop_completion_owner_v1') is True
