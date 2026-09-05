"""Tile-server access tokens (nginx ``secure_link``) for the linux_host.

Optional short-lived signed tokens restrict tile-server access. When no secrets are
configured the tile server is fully public and the generated nginx config is byte-identical
to the no-auth case: no ``$ofm_secret`` map is written, no ``secure_link`` directives are
emitted and no location carries the guard.

Under the jsonc-config architecture the secrets are deliberately NOT part of the uploaded,
schema-validated ``config.jsonc`` (which is a deploy-time artifact, often baked into a golden
AMI). Instead they live in a mutable runtime state file
``/data/ofm/linux_host/state/tile_auth_secrets.json`` that is the single source of truth on
the host and can be changed without a redeploy:

- ``ofm-tile-auth.service`` ingests a ``TILE_AUTH_SECRETS`` value from EC2 user data at boot
  (see :mod:`linux_host.linux_host_lib.user_data`);
- ``rotate-tile-auth-secrets.sh`` rotates the secret across a running fleet.

Keeping several ids configured at once enables zero-downtime rotation (old ids stay valid
until their tokens have expired).
"""

import json
import re
import sys
from pathlib import Path

from linux_host.linux_host_lib.linux_host_config import get_linux_host_config


# kids and secrets are restricted to the URL-safe base64url alphabet. This structurally
# forbids commas, colons, spaces and quotes inside a secret (so the kid:secret,... form the
# operator writes needs no escaping) and keeps every secret safe to embed verbatim in the
# generated nginx `map` (whose entries are quoted strings) and in the signed
# `"<expires> <secret>"` string. Must match the same constraint on the git-sail side.
_TILE_AUTH_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def tile_auth_state_file() -> Path:
    return get_linux_host_config().linux_host_dir / 'state' / 'tile_auth_secrets.json'


def get_tile_auth_secrets() -> dict[str, str]:
    """Return the configured tile-server auth secrets as a ``{kid: secret}`` dict.

    Source is the runtime state file written by :func:`set_tile_auth_secrets`. When it is
    absent or empty, tile-server authentication is disabled. Every kid and secret must match
    the URL-safe base64url alphabet ``[A-Za-z0-9_-]``; anything else aborts rather than
    emitting a broken nginx ``map``.
    """
    state_file = tile_auth_state_file()
    try:
        raw = json.loads(state_file.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        sys.exit(f'cannot read tile-auth state file {state_file}: {e}')

    result: dict[str, str] = {}
    for kid, secret in (raw or {}).items():
        kid = str(kid)
        secret = str(secret)
        if not _TILE_AUTH_TOKEN_RE.match(kid) or not _TILE_AUTH_TOKEN_RE.match(secret):
            sys.exit(
                f'tile_auth_secrets entry for kid {kid!r} is invalid; kid and secret must '
                f'both be non-empty and match [A-Za-z0-9_-] (no commas, colons, spaces or '
                f'quotes)'
            )
        result[kid] = secret
    return result


def set_tile_auth_secrets(secrets: dict[str, str]) -> None:
    """Persist ``secrets`` (a ``{kid: secret}`` dict; empty = public) to the state file.

    An empty dict removes the state file so the tile server is public.
    """
    state_file = tile_auth_state_file()
    if not secrets:
        state_file.unlink(missing_ok=True)
        return
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(secrets, indent=2, ensure_ascii=False) + '\n')


def parse_tile_auth_secrets(raw: str) -> dict[str, str]:
    """Parse a ``TILE_AUTH_SECRETS`` ``kid:secret,...`` string into a ``{kid: secret}`` dict.

    The value is a comma-separated list of ``kid:secret`` pairs; the comma separates entries
    and the first colon separates a kid from its secret, so both kids and secrets must match
    the URL-safe base64url alphabet ``[A-Za-z0-9_-]`` -- no commas, colons, spaces or quotes,
    and there is deliberately no escaping. A blank value yields an empty dict (auth disabled);
    any malformed entry raises ``ValueError``.
    """
    result: dict[str, str] = {}
    raw = (raw or '').strip()
    if not raw:
        return result
    for entry in raw.split(','):
        entry = entry.strip()
        colon_index = entry.find(':')
        if colon_index <= 0 or colon_index >= len(entry) - 1:
            raise ValueError(
                f'TILE_AUTH_SECRETS entry {entry!r} is malformed; expected kid:secret '
                f'(comma-separated), with kid and secret both matching [A-Za-z0-9_-]'
            )
        kid = entry[:colon_index]
        secret = entry[colon_index + 1 :]
        if not _TILE_AUTH_TOKEN_RE.match(kid) or not _TILE_AUTH_TOKEN_RE.match(secret):
            raise ValueError(
                f'TILE_AUTH_SECRETS entry {entry!r} has an invalid kid or secret; both must '
                f'be non-empty and match [A-Za-z0-9_-] (no commas, colons, spaces or quotes)'
            )
        if kid in result:
            raise ValueError(f'TILE_AUTH_SECRETS contains duplicate kid {kid!r}')
        result[kid] = secret
    return result


def secure_link_guard() -> str:
    """Return the per-location ``secure_link`` guard + CORS preflight, or '' when disabled.

    Inserted right after each protected location's opening brace. ``$secure_link`` is nginx's
    built-in verdict: ``''`` for a missing/malformed token, ``'0'`` for a bad or expired one
    and ``'1'`` for a valid, unexpired one; both ``''`` and ``'0'`` are rejected. The OPTIONS
    preflight short-circuits with 204 before any token is required (the token headers make the
    browser send a CORS preflight). The 401 carries ``Access-Control-Allow-Origin`` because the
    app runs on a different host than the tile server, so without it the cross-origin browser
    would see an opaque error instead of a 401 and could not refresh the token.
    """
    if not get_tile_auth_secrets():
        return ''
    return (
        '\n'
        "        if ($request_method = 'OPTIONS') {\n"
        "            add_header 'Access-Control-Allow-Origin' '*' always;\n"
        "            add_header 'Access-Control-Allow-Methods' 'GET, OPTIONS' always;\n"
        "            add_header 'Access-Control-Allow-Headers' '*' always;\n"
        "            add_header 'Access-Control-Max-Age' 86400 always;\n"
        '            return 204;\n'
        '        }\n'
        '        # tile-server access token (secure_link) required; secret via $ofm_secret map\n'
        "        if ($secure_link = '') {\n"
        "            add_header 'Access-Control-Allow-Origin' '*' always;\n"
        '            return 401;\n'
        '        }\n'
        "        if ($secure_link = '0') {\n"
        "            add_header 'Access-Control-Allow-Origin' '*' always;\n"
        '            return 401;\n'
        '        }\n'
    )


def secure_link_server_directives() -> str:
    """Return the server-level ``secure_link`` directives, or '' when auth is disabled.

    ``secure_link`` reads the signature and expiry from the ``X-OFM-Md5`` / ``X-OFM-Expires``
    request headers (so tile URLs stay cacheable), and ``secure_link_md5`` recomputes
    ``md5("<expires> <secret>")`` with the secret selected by the http-level ``$ofm_secret``
    map keyed on the ``X-OFM-Kid`` header.
    """
    if not get_tile_auth_secrets():
        return ''
    return (
        '    # short-lived tile-server access tokens; secret selected by the $ofm_secret map\n'
        '    # in /data/nginx/config/ofm_secure_link.conf; token travels in request headers\n'
        '    secure_link $http_x_ofm_md5,$http_x_ofm_expires;\n'
        '    secure_link_md5 "$secure_link_expires $ofm_secret";\n'
    )


def secure_link_map_path() -> Path:
    return get_linux_host_config().nginx_config_dir / 'ofm_secure_link.conf'


def secure_link_map_text() -> str | None:
    """Return the http-level ``$ofm_secret`` map, or ``None`` when auth is disabled.

    The map turns the client-sent ``X-OFM-Kid`` header into the matching signing secret; an
    unknown or absent kid maps to the empty default secret, which makes ``$secure_link``
    evaluate to ``''``/``'0'`` so the guard rejects the request.
    """
    secrets = get_tile_auth_secrets()
    if not secrets:
        return None
    lines = [
        '# Auto-generated by linux_host_lib/tile_auth.py - do not edit by hand.',
        '# Selects the tile-server signing secret from the client-sent X-OFM-Kid header so',
        '# secure_link_md5 can validate the token. Unknown/absent kid -> empty secret ->',
        '# $secure_link is ""/"0" -> the per-location guard returns 401.',
        'map $http_x_ofm_kid $ofm_secret {',
        '    default "";',
    ]
    for kid, secret in secrets.items():
        lines.append(f'    "{kid}" "{secret}";')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def write_secure_link_map() -> bool:
    """(Re)write the http-level ``$ofm_secret`` map. Returns True if the file changed.

    Written into ``/data/nginx/config/`` which ``nginx.conf`` includes at http level before
    the server blocks. When no secrets are configured the file is removed, disabling
    tile-server auth entirely.
    """
    map_path = secure_link_map_path()
    desired = secure_link_map_text()

    try:
        existing = map_path.read_text()
    except FileNotFoundError:
        existing = None

    if desired is None:
        if existing is not None:
            map_path.unlink(missing_ok=True)
            return True
        return False

    if existing == desired:
        return False

    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(desired)
    return True
