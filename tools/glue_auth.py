#!/usr/bin/env python3
"""Glue Cognito authentication, secure account-scoped token cache and key refill.

Credentials: environment or glue_store (OS vault / encrypted headless store).
Commands: token, refresh, keys, command-list, refill. Refill acknowledges durable
pending burns before downloading a fresh pool. No plaintext token cache is used.
The backend remains required to replenish the finite single-use key supply.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import quote, urlsplit
from glue_storage import locked

sys.path.insert(0, os.path.dirname(__file__))
try:
    import glue_constants as _C
    _DEF_API, _DEF_POOL, _DEF_CLIENT, _DEF_SECRET, _DEF_REGION = (
        _C.API_BASE, _C.COGNITO_POOL, _C.COGNITO_CLIENT, _C.COGNITO_SECRET, _C.AWS_REGION)
except Exception:  # constants module optional; env can still supply everything
    _DEF_API = "https://mobile.gluehome.com/api/v1"
    _DEF_POOL = _DEF_CLIENT = _DEF_SECRET = ""
    _DEF_REGION = "eu-west-1"

try:
    import glue_store  # cross-platform secret store (keyring -> file -> env)
except Exception:
    glue_store = None

API_BASE = os.environ.get("GLUE_API_BASE", _DEF_API)


def _env(name, default=None, required=False, store_key=None):
    """Resolve a value: env var, else the cross-platform store (by store_key), else default."""
    v = os.environ.get(name)
    if not v and glue_store is not None and store_key:
        try:
            v = glue_store.get(store_key)
        except Exception:
            v = None
    if not v:
        v = default
    if required and not v:
        sys.exit(f"ERROR: {name} not set (env {name}, or run `glue setup`)")
    return v


def _secret_hash(username, client_id, client_secret):
    """Cognito SECRET_HASH = Base64(HMAC-SHA256(username + clientId, clientSecret))."""
    msg = (username + client_id).encode("utf-8")
    key = client_secret.encode("utf-8")
    return base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()


def _jwt_exp(token):
    """Return the exp (epoch) claim of a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- token cache
def _cache_scope(cfg):
    identity = [cfg.get(k, '') for k in ('user', 'pool', 'client', 'region')]
    identity.append(API_BASE)
    # Changing credentials must also invalidate the cache; never store the password itself here.
    identity.append(hashlib.sha256(json.dumps([cfg.get('password', ''), cfg.get('secret', '')]).encode()).hexdigest())
    return 'token-' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _load_cache(scope):
    value = glue_store.get(scope)
    return json.loads(value) if value else {}


def _save_cache(scope, data):
    try:
        glue_store.set_(scope, json.dumps(data))
    except Exception:
        print('WARN: secure token cache unavailable; next invocation will authenticate again', file=sys.stderr)


# ---------------------------------------------------------------- cognito auth
def _cognito_client(region):
    try:
        import boto3
    except ImportError:
        sys.exit("ERROR: boto3 is required (pip install boto3)")
    return boto3.client("cognito-idp", region_name=region)


def _auth_password(cog, cfg):
    """USER_PASSWORD_AUTH — one call. Works iff ALLOW_USER_PASSWORD_AUTH on the app client."""
    return cog.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=cfg["client"],
        AuthParameters={
            "USERNAME": cfg["user"],
            "PASSWORD": cfg["password"],
            "SECRET_HASH": _secret_hash(cfg["user"], cfg["client"], cfg["secret"]),
        },
    )


def _auth_srp(cog, cfg):
    """USER_SRP_AUTH fallback — the SDK's real flow. Uses pycognito (maintained) for
    the SRP math; falls back to warrant if that's what's installed."""
    AWSSRP = None
    try:
        from pycognito.aws_srp import AWSSRP  # maintained successor to warrant
    except ImportError:
        try:
            from warrant.aws_srp import AWSSRP  # legacy; broken on py>=3.10
        except ImportError:
            sys.exit(
                "ERROR: pool rejected USER_PASSWORD_AUTH and SRP needs a helper.\n"
                "       pip install pycognito"
            )
    srp = AWSSRP(
        username=cfg["user"], password=cfg["password"],
        pool_id=cfg["pool"], client_id=cfg["client"],
        client_secret=cfg["secret"], client=cog,
    )
    return {"AuthenticationResult": srp.authenticate_user()["AuthenticationResult"]}


def _refresh(cog, cfg, refresh_token):
    return cog.initiate_auth(
        AuthFlow="REFRESH_TOKEN_AUTH",
        ClientId=cfg["client"],
        AuthParameters={
            "REFRESH_TOKEN": refresh_token,
            "SECRET_HASH": _secret_hash(cfg["user"], cfg["client"], cfg["secret"]),
        },
    )


def get_token(cfg, force=False):
    scope = _cache_scope(cfg)
    with locked(scope):
        return _get_token_locked(cfg, scope, force)


def _get_token_locked(cfg, scope, force=False):
    """Return a token for this exact identity/configuration only."""
    cache = {} if force else _load_cache(scope)
    now = int(time.time())
    at = cache.get('access_token')
    if at and _jwt_exp(at) - now > 60:
        return at
    cog = _cognito_client(cfg['region'])
    rt = cache.get('refresh_token')
    if rt and not force:
        try:
            r = _refresh(cog, cfg, rt)['AuthenticationResult']
            at = r['AccessToken']
            _save_cache(scope, {'access_token': at, 'refresh_token': r.get('RefreshToken', rt)})
            return at
        except Exception:
            print('INFO: refreshing failed; authenticating again', file=sys.stderr)
    try:
        res = _auth_password(cog, cfg)
    except Exception as e:
        msg = str(e)
        if 'USER_PASSWORD_AUTH' in msg or 'InvalidParameter' in msg or 'flow' in msg.lower():
            res = _auth_srp(cog, cfg)
        else:
            raise
    r = res['AuthenticationResult']
    at = r['AccessToken']
    _save_cache(scope, {'access_token': at, 'refresh_token': r.get('RefreshToken')})
    return at


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Refusing to redirect an authenticated request')


def _api_get(path, token, method='GET'):
    if urlsplit(API_BASE).scheme != 'https':
        raise ValueError('GLUE_API_BASE must use HTTPS')
    req = urllib.request.Request(
        f'{API_BASE.rstrip("/")}/{path}', method=method,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json',
                 'glue-requires-authorization': 'true', 'User-Agent': 'glue-local/2.0'},
    )
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=15) as resp:
        body = resp.read()
        return json.loads(body) if body else None


def api_request(cfg, path, method='GET'):
    try:
        return _api_get(path, get_token(cfg), method)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    return _api_get(path, get_token(cfg, force=True), method)


def report_burn(cfg, lock_id, index):
    """Recovered SDK DELETE endpoint. 404 alone is not proof of acknowledgement."""
    path = 'glue-osm-devices/' + quote(lock_id, safe='') + '/keys/'
    try:
        api_request(cfg, path + str(index), 'DELETE')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Retry after a lost DELETE reply: require an authenticated, valid pool
        # response showing that this index is absent before acknowledging it.
        from glue_keys import validate_keys
        keys = validate_keys(lock_id, api_request(cfg, path))
        if any(k['keyIndex'] == index for k in keys):
            raise RuntimeError('Server has not acknowledged consumed key') from None


def config():
    return {
        'user': _env('GLUE_USERNAME', store_key='username', required=True),
        'password': _env('GLUE_PASSWORD', store_key='password', required=True),
        'pool': _env('GLUE_COGNITO_POOL', _DEF_POOL, store_key='cognito-pool'),
        'client': _env('GLUE_COGNITO_CLIENT', _DEF_CLIENT, store_key='cognito-client'),
        'secret': _env('GLUE_COGNITO_SECRET', _DEF_SECRET, store_key='cognito-secret'),
        'region': _env('GLUE_REGION', _DEF_REGION),
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'token'
    if cmd not in ('token', 'refresh', 'keys', 'command-list', 'refill'):
        sys.exit('usage: auth token|refresh|keys|command-list|refill')
    cfg = config()
    if cmd in ('token', 'refresh'):
        print(get_token(cfg, force=cmd == 'refresh'))
        return
    from glue_keys import identity, KeyBank
    lock_id, _ = identity()
    if cmd == 'refill':
        print(f'{KeyBank(lock_id).sync(cfg)} unused keys banked')
    else:
        suffix = 'keys/' if cmd == 'keys' else 'command-list'
        print(json.dumps(api_request(cfg, 'glue-osm-devices/' + quote(lock_id, safe='') + '/' + suffix), indent=1))


if __name__ == "__main__":
    main()