"""Real class allocation to a disposable loopback server, never a live paid endpoint."""
import json
from urllib.parse import urlsplit


def configure(home, config, url, model='loop-model'):
    assert urlsplit(url).hostname == '127.0.0.1', url
    candidates = {model, model + '-alternate'}
    # The generic custom transport is classified metered, not a native subscription.
    # This permission is ONLY in the disposable probe home, with dummy credentials
    # and a literal loopback URL; it cannot authorize external/paid traffic.
    config['model_routing'] = {
        'enabled': True, 'policy_version': 1, 'allow_metered': True,
        'routes': {'standard': [
            {'provider': 'custom', 'model': name, 'billing': 'metered', 'weight': 1,
             'eligible': True, 'funding_verified': True,
             'capabilities': {'tools': True, 'streaming': True, 'context_tokens': 200000}}
            for name in sorted(candidates)]}}
    (home / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {'custom': [
        {'id': 'loopback', 'source': 'manual', 'auth_type': 'api_key',
         'access_token': 'loopback-only', 'base_url': url}]}}), encoding='utf-8')
    from hermes_cli.kanban_model_tier import class_unroutable_reason
    assert class_unroutable_reason('class:standard', config) is None
    return candidates
