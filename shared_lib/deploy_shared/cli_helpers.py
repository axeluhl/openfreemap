import os
from collections.abc import Callable
from typing import Any

import click
from fabric import Config, Connection
from invoke.exceptions import UnexpectedExit


DEFAULT_SSH_USER = 'ec2-user'


def resolve_deploy_targets(
    jsonc_data: dict[str, Any], hostname: str | None, user: str | None
) -> list[tuple[str, str]]:
    """Resolve the ``(host, ssh_user)`` deploy targets.

    The config describes the *type* of process and setup, not the volatile deploy IP. A
    command-line ``--host`` (optionally given as ``user@host``) is therefore authoritative:
    it is used as-is and need NOT appear in the config's ``hosts`` list, which may be empty
    or missing. Any config ``hosts`` that do not match are simply ignored. Only when no
    ``--host`` is given do we fall back to the config ``hosts``.

    SSH user precedence: ``user@`` in ``--host`` > ``--user`` > config ``ssh_user`` >
    ``ec2-user``.
    """
    config_user = jsonc_data.get('ssh_user')

    if hostname:
        host_user, sep, host = hostname.rpartition('@')
        if sep and host_user:
            return [(host, host_user)]
        return [(hostname, user or config_user or DEFAULT_SSH_USER)]

    default_user = user or config_user or DEFAULT_SSH_USER
    hosts = jsonc_data.get('hosts') or []
    if not hosts:
        raise click.ClickException(
            'No deploy host. Pass --host [user@]host, or add "hosts" to the config.'
        )
    if len(hosts) > 1:
        raise click.ClickException(
            'The config contains multiple hosts. Select one with --host to avoid downtime.'
        )
    return [(hosts[0], default_user)]


def get_connection(hostname: str, user: str | None, port: int | None) -> Connection:
    ssh_password = os.getenv('SSH_PASSWD')
    sudo_password = os.getenv('SUDO_PASSWD', ssh_password)

    connect_kwargs: dict[str, Any] = {}
    if ssh_password:
        print('Using SSH password')
        connect_kwargs = {
            'password': ssh_password,
            'allow_agent': False,
            'look_for_keys': False,
        }

    config = None
    if sudo_password:
        config = Config(overrides={'sudo': {'password': sudo_password}, 'run': {'pty': True}})

    c = Connection(
        host=hostname,
        user=user,
        port=port,
        forward_agent=True,
        connect_kwargs=connect_kwargs,
        config=config,
    )
    check_sudo(c, sudo_password=bool(sudo_password))
    return c


def check_sudo(c: Connection, *, sudo_password: bool) -> None:
    if c.run('id -u', hide=True).stdout.strip() == '0':
        if c.run('command -v sudo', hide=True, warn=True).ok:
            return
        raise click.ClickException(
            'Root SSH user is missing sudo. Install sudo on the server first.'
        )

    if sudo_password:
        try:
            c.sudo('true', hide=True)
        except UnexpectedExit as e:
            raise click.ClickException(
                'SSH user could not run sudo with the provided password. Check that the user is '
                + 'in the sudo group and that SUDO_PASSWD/SSH_PASSWD is correct.'
            ) from e
        return

    if c.run('sudo -n true', hide=True, warn=True).ok:
        return

    raise click.ClickException(
        'SSH user cannot run passwordless sudo. Use a root SSH user, configure NOPASSWD sudo, '
        + 'or set SUDO_PASSWD/SSH_PASSWD for password-based sudo.'
    )


def common_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to define common deploy arguments and options."""
    decorated = click.option(
        '--host',
        'hostname',
        help='Deploy to this host as [user@]host. Authoritative: used as-is and need not appear '
        'in the config hosts.',
    )(func)
    decorated = click.option(
        '--config', 'config_name', required=True, help='Config name without .jsonc'
    )(decorated)
    decorated = click.option('--port', type=int, help='SSH port (if not in .ssh/config)')(decorated)
    decorated = click.option(
        '--user', help=f'SSH user (defaults to config ssh_user or {DEFAULT_SSH_USER})'
    )(decorated)
    decorated = click.option(
        '-y', '--noninteractive', is_flag=True, help='Skip confirmation questions'
    )(decorated)
    return decorated
