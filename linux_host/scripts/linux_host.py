#!/usr/bin/env -S uv run python -P

from datetime import UTC, datetime

import click

from linux_host.linux_host_lib.sync import full_sync
from linux_host.linux_host_lib.telegram_alerts import send_telegram
from linux_host.linux_host_lib.user_data import (
    apply_user_data_tile_auth,
    set_tile_auth_secrets_from_stdin,
)


@click.group()
def cli() -> None:
    """Manage OpenFreeMap linux_host servers."""


@cli.command()
@click.option(
    '--download',
    'force_download',
    is_flag=True,
    help='Download from upstream even if the config sets "local_versions": true. Used by the '
    'golden-AMI bake to fetch the tiles the fleet will later serve locally.',
)
def sync(force_download: bool) -> None:
    """Run the complete host sync task."""
    print(f'---\n{datetime.now(UTC)}\nStarting sync')
    try:
        full_sync(force_download=force_download)
    except Exception as e:
        # Cloudflare read errors are handled inside full_sync. Anything else alerts
        # on every run until fixed.
        send_telegram(f'sync failed: {type(e).__name__}: {e}')
        raise

    # Final line of the run, so it is the last thing in sync.log / on the attached tmux
    # pane. A `tail -f` or `tmux attach` watcher keeps running after the sync itself is
    # done, with no other signal that it finished -- this banner is that signal.
    print(
        f'===\n{datetime.now(UTC)}\n'
        'SUCCESS: sync finished. The host is serving tiles.\n'
        'Nothing more will be printed for this run; you can press Ctrl-C to stop watching.'
    )


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
