"""Boot-time ingestion of the tile-auth secret from EC2 user data.

A golden AMI is baked *without* any tile-auth secret (see docs/self_hosting.md): rotating or
configuring secrets should not require re-baking an image, and a secret should never be
captured in an AMI. Instead the secret is handed to each instance at launch through EC2
*user data*, e.g. a single line::

    TILE_AUTH_SECRETS=k1:AbC-123_xyz,k2:Def-456_uvw

On boot ``ofm-tile-auth.service`` runs ``apply_user_data_tile_auth`` (via
``http_host.py apply-user-data-secrets``) *before* nginx starts (the unit is ordered
``Before=nginx.service`` and pulled in by ``multi-user.target``). It is **non-destructive**:

- ``TILE_AUTH_SECRETS`` absent from user data -> leave the existing config as-is (a public AMI
  stays public; a host secured via ``.env`` keeps its secret across reboots);
- ``TILE_AUTH_SECRETS=k1:...`` -> ingest it into ``config.json`` and regenerate the nginx
  config with the short-lived-token guard active;
- ``TILE_AUTH_SECRETS=`` (explicitly empty) -> clear it and serve public.

The unit never blocks nginx from starting: on a non-EC2 host or a transient IMDS failure it
leaves the config untouched and exits, and the dependency on nginx is ordering-only (no hard
``Requires=``), so a deploy-time or manual ``systemctl restart nginx`` never cascades. A
*malformed* ``TILE_AUTH_SECRETS`` exits non-zero (the unit shows failed, for visibility) but
still lets nginx come up from the existing config.
"""

import json
import re
import sys
import urllib.error
import urllib.request

from http_host_lib.config import config
from http_host_lib.nginx import parse_tile_auth_secrets, write_nginx_config


# IMDS is reachable link-local on every EC2 instance; no internet is required.
IMDS_BASE = 'http://169.254.169.254'
IMDS_TIMEOUT = 2


def _imds_token() -> str:
    """Fetch an IMDSv2 session token (required on instances with IMDSv2 enforced)."""
    req = urllib.request.Request(
        f'{IMDS_BASE}/latest/api/token',
        method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'},
    )
    with urllib.request.urlopen(req, timeout=IMDS_TIMEOUT) as resp:
        return resp.read().decode('utf-8').strip()


def fetch_ec2_user_data():
    """Return the instance's raw user data as a string, or ``None`` if there is none.

    Uses IMDSv2 (token first, then the ``/latest/user-data`` endpoint). A 404 from the
    endpoint means no user data was supplied and maps to ``None``. Any other IMDS/network
    error propagates so the caller can decide (the boot path treats it as "no user data" and
    keeps the baked config, rather than failing nginx).
    """
    token = _imds_token()
    req = urllib.request.Request(
        f'{IMDS_BASE}/latest/user-data',
        headers={'X-aws-ec2-metadata-token': token},
    )
    try:
        with urllib.request.urlopen(req, timeout=IMDS_TIMEOUT) as resp:
            return resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def extract_env_var(user_data: str, name: str):
    """Return the value of a ``NAME=value`` assignment in ``user_data``.

    Distinguishes three cases so the caller can behave non-destructively:

    - ``None`` -- ``NAME`` is **not present** in the user data at all;
    - ``''`` -- ``NAME=`` is present but **empty** (an explicit request to clear);
    - a non-empty string -- the assigned value.

    Scans line by line for ``NAME=...`` (optionally ``export NAME=...``), so it works whether
    the user data is a bare env file or the variable is set inside a shell script. Surrounding
    single/double quotes are stripped and the last matching line wins.
    """
    if not user_data:
        return None
    pattern = re.compile(rf'^\s*(?:export\s+)?{re.escape(name)}=(.*)$')
    value = None
    for line in user_data.splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1).strip()
    if value is None:
        return None
    if len(value) >= 2 and value[0] in '"\'' and value[-1] == value[0]:
        value = value[1:-1]
    return value.strip()


def _persist_config():
    """Write the in-memory config back to ``config.json`` (secrets are not logged)."""
    config_path = config.ofm_config_dir / 'config.json'
    config_path.write_text(json.dumps(config.ofm_config, indent=2, ensure_ascii=False) + '\n')


def _apply_parsed_secrets(secrets: dict):
    """Store ``secrets`` (a ``{kid: secret}`` dict; empty = public) and regenerate nginx.

    Updates ``config.json`` in place -- setting ``tile_auth_secrets`` when secrets are given,
    or dropping the key when the dict is empty so the running config matches the "open server"
    intent -- then calls ``write_nginx_config()``, which rewrites the map/guards and reloads a
    running nginx gracefully (SIGHUP, existing connections finish on the old workers). Shared by
    the boot-time user-data path and the live-rotation ``set-tile-auth-secrets`` command.
    """
    if secrets:
        config.ofm_config['tile_auth_secrets'] = secrets
        _persist_config()
    elif config.ofm_config.pop('tile_auth_secrets', None) is not None:
        _persist_config()
    write_nginx_config()


def set_tile_auth_secrets_from_stdin(allow_clear: bool = False):
    """Read a ``TILE_AUTH_SECRETS`` value from stdin and apply it, then reload nginx.

    Used by the live rotation script (``rotate-tile-auth-secrets.sh``) to rotate the secret on
    already-running instances without replacing them. The value is read from **stdin** (not a
    CLI argument) so it never lands on the process list. An empty input is refused unless
    ``allow_clear`` is set, to avoid accidentally turning a protected server public.

    Note: this changes the *running* config only. On the next reboot, ``ofm-tile-auth.service``
    re-reads the instance's EC2 user data, so for a durable rotation also update the launch
    template's user data (see docs/self_hosting.md).
    """
    raw = sys.stdin.read().strip()
    if not raw:
        if not allow_clear:
            sys.exit(
                'refusing to clear tile-auth secrets: empty input. Pass --clear to make the '
                'tile server public.'
            )
        print('clearing tile-auth secrets; tile server will be public')
        _apply_parsed_secrets({})
        return
    try:
        secrets = parse_tile_auth_secrets(raw)
    except ValueError as e:
        sys.exit(f'invalid TILE_AUTH_SECRETS: {e}')
    print(f'setting tile-auth secrets; kid(s): {", ".join(secrets)}')
    _apply_parsed_secrets(secrets)


def apply_user_data_tile_auth():
    """Ingest ``TILE_AUTH_SECRETS`` from EC2 user data and (re)write the nginx config.

    Called at boot by ``ofm-tile-auth.service`` before nginx starts. Non-destructive by design
    (see the module docstring): the tile-auth config is only touched when user data explicitly
    says so, so this never clobbers a secret configured another way (e.g. via ``.env`` on a
    directly-managed host) and never blocks nginx from starting.
    """
    print('Applying tile-auth secrets from EC2 user data (if any)')

    try:
        user_data = fetch_ec2_user_data()
    except Exception as e:  # noqa: BLE001 - IMDS/network errors must never block nginx
        print(
            f'  could not read EC2 user data ({e}); leaving the existing tile-auth config '
            f'unchanged',
            file=sys.stderr,
        )
        return

    raw = extract_env_var(user_data, 'TILE_AUTH_SECRETS')

    if raw is None:
        # TILE_AUTH_SECRETS is not mentioned in user data at all -> leave the existing config
        # untouched. With the recommended public AMI this simply keeps the server public; on a
        # host secured via .env it preserves that secret across reboots.
        print('  no TILE_AUTH_SECRETS in user data; leaving the existing tile-auth config as-is')
        return

    if raw == '':
        # Explicit empty value (TILE_AUTH_SECRETS=) -> operator asked to clear it -> public.
        print('  TILE_AUTH_SECRETS is empty in user data; clearing tile-auth (server public)')
        _apply_parsed_secrets({})
        return

    try:
        secrets = parse_tile_auth_secrets(raw)
    except ValueError as e:
        # An explicit but malformed value is an operator error; surface it loudly (the unit
        # shows failed in `systemctl status`). nginx still starts from the existing config.
        sys.exit(f'  invalid TILE_AUTH_SECRETS in EC2 user data: {e}')

    print(f'  enabling tile-server auth from user data; kid(s): {", ".join(secrets)}')
    _apply_parsed_secrets(secrets)
