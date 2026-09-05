#!/usr/bin/env -S uv run python -P

from pathlib import Path
from typing import Any

import click

from linux_host.deploy_linux_host.linux_host_deploy_config import linux_host_deploy_config
from linux_host.deploy_linux_host.tasks_linux_host import (
    clean_linux_host,
    copy_runs_from_host,
    install_linux_host_cron,
    mount_nvme_download_volume,
    prepare_linux_host,
    run_linux_host_sync_detached,
)
from linux_host.linux_host_lib.config_loader import (
    read_linux_host_jsonc_config,
    resolve_upload_cert_paths,
)
from shared_lib.deploy_shared.cli_helpers import (
    common_options,
    get_connection,
    resolve_deploy_targets,
)
from shared_lib.deploy_shared.tasks_shared import prepare_shared


@click.command()
@common_options
@click.option(
    '--copy-runs-from-host',
    'copy_runs_src_host',
    help='Seed the btrfs runs by scp from this already-provisioned host instead of downloading '
    'them (fast golden-AMI bake with unchanged tiles). Requires local_versions in the config.',
)
@click.option(
    '--copy-runs-user',
    help='SSH user for --copy-runs-from-host (defaults to the target --user / ssh login user).',
)
@click.option(
    '--copy-runs-src-dir',
    help="Runs directory on the --copy-runs-from-host source (default: this host's "
    '/data/ofm/linux_host/versions). Use /data/ofm/http_host/runs to seed from an old-layout host.',
)
@click.option(
    '--no-nvme',
    is_flag=True,
    help='Do not stage the btrfs download on a local ephemeral NVMe; download onto the versions '
    'volume instead.',
)
@click.option(
    '--nvme-min-size-gb',
    type=int,
    default=100,
    show_default=True,
    help='Minimum unformatted NVMe size (GB) to qualify as the download staging volume, sized for '
    'the ~90 GB gzipped planet download.',
)
def deploy(
    config_name: str,
    hostname: str | None,
    user: str | None,
    port: int | None,
    noninteractive: bool,
    copy_runs_src_host: str | None,
    copy_runs_user: str | None,
    copy_runs_src_dir: str | None,
    no_nvme: bool,
    nvme_min_size_gb: int,
) -> None:
    jsonc_path, jsonc_data = load_jsonc_config(config_name)
    validate_local_cert_files(jsonc_path, jsonc_data)
    if copy_runs_src_host and not jsonc_data.get('local_versions'):
        raise click.ClickException(
            '--copy-runs-from-host requires "local_versions": true in the config.\n'
            'Copied runs are served as-is; without local_versions the next sync would follow the '
            'upstream deployed-version pointer and re-download tiles, defeating the copy.'
        )
    targets = resolve_deploy_targets(jsonc_data, hostname, user)
    if not confirm_hosts([host for host, _ in targets], noninteractive):
        return

    for host, ssh_user in targets:
        c = get_connection(host, ssh_user, port)
        clean_linux_host(c, jsonc_data['areas'])
        prepare_shared(c, linux_host_deploy_config)
        prepare_linux_host(c, jsonc_path)
        if copy_runs_src_host:
            # Runs are copied straight onto the versions volume; no download, so no NVMe staging.
            copy_runs_from_host(
                c, copy_runs_src_host, copy_runs_user or ssh_user, copy_runs_src_dir
            )
        elif not no_nvme:
            mount_nvme_download_volume(c, min_size_gb=nvme_min_size_gb)
        if jsonc_data['auto_update']:
            install_linux_host_cron(c)
            click.echo(f'Automatic sync scheduled on {host}.')
        else:
            run_linux_host_sync_detached(c, host)
        print_success_message(jsonc_data)


def validate_local_cert_files(jsonc_path: Path, jsonc_data: dict[str, Any]) -> None:
    # Validate every upload before opening any SSH connection. Direct upload then
    # needs no remote staging and invalid local input cannot change a replica.
    for domain_data in jsonc_data['domains']:
        cert = domain_data['cert']
        if cert['type'] != 'upload':
            continue

        cert_path, key_path = resolve_upload_cert_paths(jsonc_path, cert['cert_path'])
        if not cert_path.is_file() or not key_path.is_file():
            raise click.ClickException(
                f'Certificate or key file for {domain_data["domain"]} was not found.\n'
                f'Make sure these files exist:\n{cert_path}\n{key_path}'
            )


def confirm_hosts(hosts: list[str], noninteractive: bool) -> bool:
    return noninteractive or click.confirm(f'Run on {", ".join(hosts)}?')


def print_success_message(jsonc_data: dict[str, Any]) -> None:
    style_url = f'https://{jsonc_data["domains"][0]["domain"]}/styles/liberty'
    click.echo()
    click.secho('linux_host setup complete.', fg='green')
    click.echo('After synchronization, use this style URL in a MapLibre map:')
    click.secho(style_url, fg='cyan')
    click.echo()


def load_jsonc_config(config_name: str) -> tuple[Path, dict[str, Any]]:
    if config_name.endswith('.jsonc'):
        raise click.ClickException(
            'Config names should not include .jsonc.\n\nExample:\n'
            '  ./linux_host/deploy_linux_host.py --config staging'
        )

    jsonc_path = linux_host_deploy_config.local_linux_host_config_dir / f'{config_name}.jsonc'
    if not jsonc_path.is_file():
        config_dir = linux_host_deploy_config.local_linux_host_config_dir
        repo_root = linux_host_deploy_config.local_repo_root
        raise click.ClickException(
            f'Config file not found:\n  {jsonc_path.relative_to(repo_root)}\n\n'
            f'Create it from the sample:\n  cp '
            f'{(config_dir / "config.sample.jsonc").relative_to(repo_root)} '
            f'{(config_dir / f"{config_name}.jsonc").relative_to(repo_root)}\n\n'
            f'Then run:\n  ./linux_host/deploy_linux_host.py --config {config_name}'
        )

    try:
        jsonc_data = read_linux_host_jsonc_config(jsonc_path)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    return jsonc_path, jsonc_data


if __name__ == '__main__':
    deploy()
