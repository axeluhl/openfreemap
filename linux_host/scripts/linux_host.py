#!/usr/bin/env -S uv run python -P

from datetime import UTC, datetime

import click

from linux_host.linux_host_lib.sync import full_sync
from linux_host.linux_host_lib.user_data import (
    apply_user_data_tile_auth,
    set_tile_auth_secrets_from_stdin,
)


@click.group()
def cli() -> None:
    """Manage OpenFreeMap linux_host servers."""


@cli.command()
def sync() -> None:
    """Run the complete host sync task."""
    print(f'---\n{datetime.now(UTC)}\nStarting sync')
    full_sync()


@cli.command('apply-user-data-secrets')
def apply_user_data_secrets() -> None:
    """Ingest TILE_AUTH_SECRETS from EC2 user data and regenerate the nginx config.

    Run at boot by ofm-tile-auth.service before nginx starts.
    """
    apply_user_data_tile_auth()


@cli.command('set-tile-auth-secrets')
@click.option('--clear', is_flag=True, help='Allow an empty stdin to clear secrets (public).')
def set_tile_auth_secrets(clear: bool) -> None:
    """Read a TILE_AUTH_SECRETS value from stdin, apply it and reload nginx (live rotation)."""
    set_tile_auth_secrets_from_stdin(allow_clear=clear)


if __name__ == '__main__':
    cli()
