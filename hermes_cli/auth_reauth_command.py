"""Interactive selected-account recovery, without append or singleton writes."""
import getpass
import sys
import warnings

from hermes_cli.auth_reauth_target import ReauthConflict, commit_reauth, prepare_reauth


def _secret_code(prompt: str) -> str:
    # getpass otherwise falls back to echoing stdin when the terminal has no
    # controllable echo. A recovery terminal must fail closed in that case.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def auth_reauth_command(args) -> None:
    """Reauthenticate an exact owned pool ID; never resolve a display label."""
    from hermes_cli import auth
    from hermes_cli.auth_commands import _OAUTH_ADD_SPECS
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('Account recovery requires an interactive private terminal.')
    committing = False
    try:
        target = prepare_reauth(args.provider, args.credential_id)
        unverified = target.provider == 'anthropic'
        if unverified:
            print('The stored grant has no verifiable account identity. This replaces the selected credential; '
                  'it cannot prove the new sign-in is the same account.')
            if input('Replace this selected credential? Type replace to continue: ').strip() != 'replace':
                print('Sign-in cancelled; no credential was changed.')
                return
            from agent.anthropic_credentials import run_hermes_oauth_login_pure
            credentials = run_hermes_oauth_login_pure(code_reader=_secret_code)
        else:
            credentials = _OAUTH_ADD_SPECS[target.provider].login(args)
        if not credentials:
            print('Sign-in cancelled or unsuccessful; no credential was changed.')
            return
        spec = _OAUTH_ADD_SPECS[target.provider]
        grant = {'access_token': spec.token(credentials), **spec.fields(credentials, target.provider)}
        committing = True
        result = commit_reauth(target, grant, allow_unverified_identity=unverified)
    except (KeyboardInterrupt, EOFError):
        print('Recovery interrupted during save; credential state is unverified. Refresh accounts before retrying.'
              if committing else 'Sign-in cancelled; no credential was changed.')
        return
    except getpass.GetPassWarning as exc:
        raise SystemExit('Secure input is unavailable; no credential was changed.') from exc
    except (ReauthConflict, auth.AuthError) as exc:
        # Provider error bodies can carry secrets. Only our fixed conflict text is
        # safe to display; provider failures retain a generic user-facing outcome.
        message = str(exc) if isinstance(exc, ReauthConflict) else 'Provider sign-in failed; retry recovery.'
        raise SystemExit(message) from None
    identity = 'Identity matched.' if result.identity_verified else 'Account identity remains unverified.'
    print(f'Selected credential saved. {identity} Account health still requires refresh and verification.')
