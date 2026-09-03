"""Boot-time ingestion of the tile-auth secret from EC2 user data.

A golden AMI is baked *without* any tile-auth secret (see docs/self_hosting.md): rotating or
configuring secrets should not require re-baking an image, and a secret should never be
captured in an AMI. Instead the secret is handed to each instance at launch through EC2
*user data*, e.g. a single line::

    TILE_AUTH_SECRETS=k1:AbC-123_xyz,k2:Def-456_uvw

On boot ``ofm-tile-auth.service`` runs ``apply_user_data_tile_auth`` (via
``http_host.py apply-user-data-secrets``) *before* nginx starts. If the variable is present it
is ingested into ``/data/ofm/config/config.json`` and the nginx config is regenerated so the
short-lived-token guard is active; if it is absent the tile server stays fully public. The unit
is deliberately resilient: on a non-EC2 host or a transient IMDS failure it leaves the baked
config untouched and still (re)writes the nginx config, so nginx always comes up. The only hard
failure is a *malformed* ``TILE_AUTH_SECRETS`` (fail closed -- an operator who asked for
protection must not silently get an open server).
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
    """Return the value of a ``NAME=value`` assignment in ``user_data``, or ``None``.

    Scans the user data line by line for ``NAME=...`` (optionally ``export NAME=...``), so it
    works whether the user data is a bare env file or the variable is set inside a shell
    script. Surrounding single/double quotes are stripped. The last matching line wins; an
    empty value is treated as absent (``None``) so an explicit ``TILE_AUTH_SECRETS=`` keeps the
    server public.
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
    value = value.strip()
    return value or None


def _persist_config():
    """Write the in-memory config back to ``config.json`` (secrets are not logged)."""
    config_path = config.ofm_config_dir / 'config.json'
    config_path.write_text(json.dumps(config.ofm_config, indent=2, ensure_ascii=False) + '\n')


def apply_user_data_tile_auth():
    """Ingest ``TILE_AUTH_SECRETS`` from EC2 user data and (re)write the nginx config.

    Called at boot by ``ofm-tile-auth.service`` before nginx starts. See the module docstring
    for the overall contract.
    """
    print('Applying tile-auth secrets from EC2 user data (if any)')

    try:
        user_data = fetch_ec2_user_data()
    except Exception as e:  # noqa: BLE001 - IMDS/network errors must never brick boot
        print(
            f'  could not read EC2 user data ({e}); leaving the baked tile-auth config '
            f'unchanged',
            file=sys.stderr,
        )
        write_nginx_config()
        return

    raw = extract_env_var(user_data, 'TILE_AUTH_SECRETS')

    if raw is None:
        # No secret handed to this instance -> serve public. Drop any stale secret that may
        # have been baked in, so the running config matches the "open server" intent.
        if config.ofm_config.pop('tile_auth_secrets', None) is not None:
            _persist_config()
        print('  no TILE_AUTH_SECRETS in user data; tile server stays public')
        write_nginx_config()
        return

    try:
        secrets = parse_tile_auth_secrets(raw)
    except ValueError as e:
        # Fail closed: the operator explicitly asked for protection but the value is broken.
        # nginx.service Requires this unit, so exiting non-zero keeps nginx from starting with
        # an unprotected config rather than silently serving open tiles.
        sys.exit(f'  invalid TILE_AUTH_SECRETS in EC2 user data: {e}')

    config.ofm_config['tile_auth_secrets'] = secrets
    _persist_config()
    print(f'  enabling tile-server auth from user data; kid(s): {", ".join(secrets)}')
    write_nginx_config()
