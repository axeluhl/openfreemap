#!/usr/bin/env python3

import click
from fabric import Config, Connection

from ssh_lib import MODULES_DIR, dotenv_val
from ssh_lib.tasks import (
    copy_runs_from_host as copy_runs_from_host_task,
    mount_nvme_data_volume,
    prepare_http_host,
    prepare_shared,
    prepare_tile_gen,
    run_http_host_sync,
    setup_loadbalancer,
    upload_config_json,
)
from ssh_lib.utils import (
    put,
)


def get_connection(hostname, user, port):
    ssh_passwd = dotenv_val('SSH_PASSWD')

    if ssh_passwd:
        print('Using SSH password')

        c = Connection(
            host=hostname,
            user=user,
            port=port,
            forward_agent=True,
            connect_kwargs={'password': ssh_passwd},
            config=Config(overrides={'sudo': {'password': ssh_passwd}}),
        )
    else:
        c = Connection(
            host=hostname,
            user=user,
            port=port,
            forward_agent=True,
        )

    return c


def common_options(func):
    """Decorator to define common options."""
    func = click.argument('hostname')(func)
    func = click.option('--port', type=int, help='SSH port (if not in .ssh/config)')(func)
    func = click.option('--user', help='SSH user (if not in .ssh/config)')(func)
    func = click.option('-y', '--noninteractive', is_flag=True, help='Skip confirmation questions')(
        func
    )
    return func


def copy_runs_options(func):
    """Options for seeding the btrfs runs (all areas) from an existing host via scp."""
    func = click.option(
        '--copy-runs-from-host',
        help='Copy the btrfs runs for all areas from this host via scp instead '
        'of downloading them from the web (source path: '
        '/data/ofm/http_host/runs)',
    )(func)
    func = click.option(
        '--copy-runs-user',
        help='SSH user for --copy-runs-from-host (defaults to the target --user / ssh login user)',
    )(func)
    return func


def nvme_options(func):
    """Options for storing /data/ofm on a local NVMe volume."""
    func = click.option(
        '--no-nvme',
        is_flag=True,
        help='Do not look for an unformatted NVMe volume to mount at /data/ofm',
    )(func)
    func = click.option(
        '--nvme-min-size-gb',
        type=int,
        default=200,
        show_default=True,
        help='Minimum size for an NVMe volume to be used for /data/ofm '
        '(must hold the uncompressed btrfs image)',
    )(func)
    return func


@click.group()
def cli():
    pass


@cli.command()
@common_options
@nvme_options
@copy_runs_options
@click.option('--domain', help='Public hostname for nginx server_name (overrides DOMAIN_DIRECT in .env)')
def http_host_static(
    hostname,
    user,
    port,
    noninteractive,
    nvme_min_size_gb,
    no_nvme,
    copy_runs_from_host,
    copy_runs_user,
    domain,
):
    if not noninteractive and not click.confirm(f'Run script on {hostname}?'):
        return

    c = get_connection(hostname, user, port)

    if not no_nvme:
        mount_nvme_data_volume(c, min_size_gb=nvme_min_size_gb)

    prepare_shared(c, domain=domain)
    prepare_http_host(c)

    if copy_runs_from_host:
        copy_runs_from_host_task(c, copy_runs_from_host, copy_runs_user or user)

    run_http_host_sync(c)


@cli.command()
@common_options
@nvme_options
@copy_runs_options
@click.option('--domain', help='Public hostname for nginx server_name (overrides DOMAIN_DIRECT in .env)')
def http_host_autoupdate(
    hostname,
    user,
    port,
    noninteractive,
    nvme_min_size_gb,
    no_nvme,
    copy_runs_from_host,
    copy_runs_user,
    domain,
):
    if not noninteractive and not click.confirm(f'Run script on {hostname}?'):
        return

    c = get_connection(hostname, user, port)

    if not no_nvme:
        mount_nvme_data_volume(c, min_size_gb=nvme_min_size_gb)

    c.sudo('rm -f /etc/cron.d/ofm_http_host')

    prepare_shared(c, domain=domain)
    prepare_http_host(c)

    if copy_runs_from_host:
        copy_runs_from_host_task(c, copy_runs_from_host, copy_runs_user or user)

    run_http_host_sync(c)  # disable for first install if you don't want to wait

    put(c, MODULES_DIR / 'http_host' / 'cron.d' / 'ofm_http_host', '/etc/cron.d/')


@cli.command()
@common_options
@click.option('--cron', is_flag=True, help='Enable cron task')
@click.option('--reinstall', is_flag=True, help='Reinstall everything in /data/ofm folder')
def tile_gen(
    hostname,
    user,
    port,
    noninteractive,
    #
    cron,
    reinstall,
):
    if not noninteractive and not click.confirm(f'Run script on {hostname}?'):
        return

    c = get_connection(hostname, user, port)

    if reinstall:
        c.sudo('rm -rf /data/ofm')

    prepare_shared(c)
    prepare_tile_gen(c, enable_cron=cron)


@cli.command()
@common_options
def loadbalancer(hostname, user, port, noninteractive):
    if not noninteractive and not click.confirm(f'Run script on {hostname}?'):
        return

    c = get_connection(hostname, user, port)
    prepare_shared(c)

    setup_loadbalancer(c)


@cli.command()
@common_options
@click.option('--domain', help='Update nginx server_name to this hostname before syncing (overrides DOMAIN_DIRECT in .env)')
def http_host_sync(hostname, user, port, noninteractive, domain):
    if not noninteractive and not click.confirm(f'Run script on {hostname}?'):
        return

    c = get_connection(hostname, user, port)
    if domain:
        upload_config_json(c, domain=domain)
    run_http_host_sync(c)


@cli.command()
@common_options
def debug(hostname, user, port, noninteractive):
    c = get_connection(hostname, user, port)
    run_http_host_sync(c)


if __name__ == '__main__':
    cli()
