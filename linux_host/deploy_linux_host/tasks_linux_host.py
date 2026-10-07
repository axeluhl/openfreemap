import shlex
import time
from pathlib import Path

import click
from fabric import Connection

from linux_host.deploy_linux_host.linux_host_deploy_config import linux_host_deploy_config
from linux_host.deploy_linux_host.nginx import configure_nginx
from linux_host.linux_host_lib.config_loader import (
    read_linux_host_jsonc_config,
    resolve_upload_cert_paths,
)
from shared_lib.ssh_lib.kernel import kernel_limits1m, kernel_somaxconn65k
from shared_lib.ssh_lib.utils import get_username, put, run_nice


def clean_linux_host(c: Connection, areas: list[str]) -> None:
    # Replicas are rebuilt offline instead of reconciling old and new runtime
    # code. Assets, ACME state, and complete images for configured areas survive.
    c.sudo('rm -f /etc/cron.d/ofm_linux_host')
    c.sudo('rm -f /etc/logrotate.d/openfreemap-nginx')
    c.sudo('systemctl disable --now ofm-tile-auth.service', warn=True, hide=True)
    c.sudo('rm -f /etc/systemd/system/ofm-tile-auth.service')
    c.sudo('systemctl daemon-reload', warn=True, hide=True)
    c.sudo('tmux kill-session -t ofm_linux_host_sync', warn=True, hide=True)
    for signal in ('TERM', 'KILL'):
        command = (
            f"for pid in $(pgrep -f '[l]inux_host.py sync'); do "
            f'pkill -{signal} -P "$pid" || true; done'
        )
        c.sudo(f'bash -c {shlex.quote(command)}', warn=True)
        c.sudo(f"pkill -{signal} -f '[l]inux_host.py sync'", warn=True)
    c.sudo('systemctl stop nginx', warn=True)
    unmounts = "findmnt -rn -o TARGET | grep '^/mnt/ofm/' | sort -r | xargs -r -n1 umount"
    c.sudo(f'bash -c {shlex.quote(unmounts)}')
    # Drop the managed /mnt/ofm/ loop-mount lines from fstab (reconcile_mounts writes them so the
    # mounts survive a reboot / AMI bake). Leaving them would make the next boot's `mount -a` fail
    # on images this clean is about to remove; the following sync rewrites them for the new runs.
    strip_fstab = "sed -i '\\#[[:space:]]/mnt/ofm/#d' /etc/fstab"
    c.sudo(f'bash -c {shlex.quote(strip_fstab)}')
    c.sudo('rm -rf /mnt/ofm')
    c.sudo('mkdir -p /mnt/ofm')

    versions_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/versions'
    c.sudo(f'mkdir -p {versions_dir}')
    keep_areas = ' '.join(f'! -name {shlex.quote(area)}' for area in areas)
    c.sudo(f'find {versions_dir} -mindepth 1 -maxdepth 1 {keep_areas} -exec rm -rf -- {{}} +')
    c.sudo(f'rm -rf {linux_host_deploy_config.remote_linux_host_dir}/runs')
    c.sudo(f'rm -rf {linux_host_deploy_config.remote_linux_host_dir}/tmp')

    c.sudo(
        f'rm -rf {linux_host_deploy_config.remote_source_dir} '
        f'{linux_host_deploy_config.remote_linux_host_config} '
        f'{linux_host_deploy_config.remote_linux_host_dir}/state '
        f'{linux_host_deploy_config.remote_linux_host_dir}/logs '
        f'{linux_host_deploy_config.remote_linux_host_dir}/logs_nginx '
        '/data/nginx/certs /data/nginx/config /data/nginx/logs /data/nginx/sites'
    )
    c.sudo('rm -f /run/lock/ofm_linux_host.lock')


def prepare_linux_host(c: Connection, jsonc_path: Path) -> None:
    jsonc_data = read_linux_host_jsonc_config(jsonc_path)
    # All-ALB hosts serve plain HTTP behind an upstream TLS terminator; the first tile vhost
    # owns the port-80 default_server (to answer ALB health checks by IP), so default_disable
    # must not be installed.
    alb_mode = all(domain_data['cert']['type'] == 'alb' for domain_data in jsonc_data['domains'])

    kernel_somaxconn65k(c)
    kernel_limits1m(c)
    configure_nginx(c, alb_mode=alb_mode)
    install_tile_auth_service(c)
    # No host firewall on AL2023: inbound access is controlled by the AWS security group
    # (port 80 from the ALB, optionally 443 for non-ALB TLS cert types). Upstream's `ufw`
    # calls do not apply here since AL2023 does not ship ufw.

    c.sudo(f'mkdir -p {linux_host_deploy_config.remote_linux_host_dir}/logs')
    c.sudo(f'chown ofm:ofm {linux_host_deploy_config.remote_linux_host_dir}/logs')

    nginx_logs_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/logs_nginx'
    c.sudo(f'mkdir -p {nginx_logs_dir}')
    c.sudo(f'chown nginx:nginx {nginx_logs_dir}')

    nginx_log_paths = ['/data/nginx/logs/nginx-error.log']
    for domain_data in jsonc_data['domains']:
        base_path = f'{nginx_logs_dir}/{domain_data["slug"]}'
        nginx_log_paths.append(f'{base_path}-access.jsonl')
    quoted_log_paths = ' '.join(shlex.quote(path) for path in nginx_log_paths)
    c.sudo(f'touch {quoted_log_paths}')
    c.sudo(f'chown nginx:adm {quoted_log_paths}')
    c.sudo(f'chmod 0640 {quoted_log_paths}')

    upload_jsonc_config_and_certs(c, jsonc_path)


def upload_jsonc_config_and_certs(c: Connection, jsonc_path: Path) -> None:
    jsonc_data = read_linux_host_jsonc_config(jsonc_path)
    c.sudo('mkdir -p /data/nginx/certs')
    c.sudo('rm -rf /data/nginx/certs/ofm-*')

    for domain_data in jsonc_data['domains']:
        if domain_data['cert']['type'] == 'upload':
            local_cert_path, local_key_path = resolve_upload_cert_paths(
                jsonc_path, domain_data['cert']['cert_path']
            )
            remote_cert_path = f'/data/nginx/certs/ofm-{domain_data["slug"]}.cert'
            remote_key_path = f'/data/nginx/certs/ofm-{domain_data["slug"]}.key'

            put(c, local_cert_path, remote_cert_path)
            put(c, local_key_path, remote_key_path)

    put(
        c,
        jsonc_path,
        f'{linux_host_deploy_config.remote_linux_host_config}/config.jsonc',
        user='ofm',
        create_parent_dir=True,
    )
    put(
        c,
        jsonc_path.parent / 'schema.json',
        f'{linux_host_deploy_config.remote_linux_host_config}/schema.json',
        user='ofm',
    )


def install_tile_auth_service(c: Connection) -> None:
    """Install and enable ofm-tile-auth.service (boot-time EC2 user-data secret ingest).

    The unit is ordered Before=nginx.service, so a TILE_AUTH_SECRETS handed to the instance
    via EC2 user data is applied before nginx serves. It is enabled (runs on next boot) but
    not started here: on a deploy to a running host it is non-destructive, and the following
    sync regenerates the nginx config from the current runtime state anyway. Also ensure the
    http-level nginx config include dir exists for the generated secure_link map.
    """
    c.sudo('mkdir -p /data/nginx/config')
    put(
        c,
        linux_host_deploy_config.local_linux_host_dir
        / 'assets'
        / 'systemd'
        / 'ofm-tile-auth.service',
        '/etc/systemd/system/ofm-tile-auth.service',
        permissions='0644',
    )
    c.sudo('systemctl daemon-reload')
    c.sudo('systemctl enable ofm-tile-auth.service')


def copy_runs_from_host(
    c: Connection, src_host: str, src_user: str | None = None, src_dir: str | None = None
) -> None:
    """Seed this host's btrfs runs from an already-provisioned host via scp.

    Instead of downloading the multi-hundred-GB ``tiles.btrfs`` for each area from the web,
    copy the existing runs from a host you already operate. This is the fast path when baking a
    fresh golden AMI with new scripts/config but the **same tiles** (minutes instead of hours).

    The runs are always placed into this host's ``/data/ofm/linux_host/versions/<area>/<version>/``
    so the subsequent ``linux_host.py sync`` in ``local_versions`` mode serves them directly with
    no download. Planet is the big one; the other areas are tiny and copied along with it.

    ``src_dir`` is the runs directory ON THE SOURCE host; it defaults to this host's versions dir.
    Point it at ``/data/ofm/http_host/runs`` to seed from an old-layout host (the inner
    ``<area>/<version>/tiles.btrfs`` layout is identical, only the base path differs).

    ``src_user`` defaults to the login user used to connect to this (target) host. The scp runs
    on the target as that login user, so SSH auth to ``src_host`` uses its forwarded ssh-agent
    (agent forwarding is enabled on the connection in ``get_connection``). Make sure your local
    ssh-agent holds a key that can reach ``src_host`` before running the deploy.
    """
    versions_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/versions'
    src_dir = src_dir or versions_dir
    login_user = get_username(c)
    src_user = src_user or login_user
    staging = f'{versions_dir}/_copy_tmp'

    print(f'Copying btrfs runs from {src_user}@{src_host}:{src_dir}')

    # /data/ofm is owned by ofm, so create a staging dir the (possibly non-ofm) login user
    # running scp is allowed to write into.
    c.sudo(f'mkdir -p {versions_dir}')
    c.sudo(f'rm -rf {staging}')
    c.sudo(f'mkdir -p {staging}')
    c.sudo(f'chown {login_user} {staging}')

    # Copy every area dir from the source runs dir into staging; the trailing /. copies the
    # contents (the <area> subdirs), not the runs dir itself.
    run_nice(
        c,
        f'scp -rp -o StrictHostKeyChecking=accept-new '
        f'{shlex.quote(f"{src_user}@{src_host}:{src_dir}")}/. {shlex.quote(staging)}/',
    )
    # The source may itself contain a leftover staging dir; drop it.
    c.sudo(f'rm -rf {staging}/_copy_tmp')

    # Move each area into place (same filesystem -> instant) and normalise ownership to match
    # files created by the regular download path. Iterate in Python, not a remote shell loop.
    area_names = c.sudo(f'ls -1 {staging}', hide=True).stdout.split()
    for name in area_names:
        c.sudo(f'rm -rf {versions_dir}/{shlex.quote(name)}')
        c.sudo(f'mv {staging}/{shlex.quote(name)} {versions_dir}/{shlex.quote(name)}')
    c.sudo(f'chown -R root:root {versions_dir}')
    c.sudo(f'rm -rf {staging}')


def assert_local_runs_present(c: Connection, areas: list[str]) -> None:
    """Fail early when local_versions serving is requested but the host holds no runs.

    ``local_versions: true`` serves runs already materialized under ``versions/<area>/``
    (golden AMI / copied runs) and downloads nothing. On a fresh host without seeded runs the
    only symptom would otherwise be the detached sync raising ``no local runs found`` and
    exiting within a second -- which tears down its tmux session before anyone can attach and
    leaves the failure buried in the sync log. Check up front instead, with a clear message.
    """
    versions_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/versions'
    missing = []
    for area in areas:
        res = c.sudo(
            f'ls {shlex.quote(versions_dir)}/{shlex.quote(area)}/*/tiles.btrfs',
            warn=True,
            hide=True,
        )
        if not res.ok:
            missing.append(area)
    if missing:
        raise click.ClickException(
            'config has "local_versions": true, which serves runs already present on the host, '
            f'but this host has no runs for: {", ".join(missing)}.\n'
            'Either set "local_versions": false to download the latest tiles from upstream, or '
            'seed the host first with --copy-runs-from-host.'
        )


def run_linux_host_sync_detached(c: Connection, hostname: str) -> None:
    log_file = f'{linux_host_deploy_config.remote_linux_host_dir}/logs/sync.log'
    inner = (
        f'cd {linux_host_deploy_config.remote_source_dir} && '
        'env PYTHONUNBUFFERED=1 ./linux_host/scripts/linux_host.py sync'
    )
    # Tee the sync's output to a persistent log so a crash (which ends the tmux session) still
    # leaves a trace; tee also keeps writing to the pane, so `tmux attach` shows live output.
    command = f'{inner} 2>&1 | tee -a {shlex.quote(log_file)}'
    c.sudo(f'tmux new-session -d -s ofm_linux_host_sync {shlex.quote(command)}')
    # Include the SSH user so the printed command works verbatim; the local username
    # (ssh's default) usually differs from the remote deploy user (e.g. ec2-user).
    target = f'{c.user}@{hostname}' if c.user else hostname
    print(f'Attach with: ssh -t {shlex.quote(target)} sudo tmux attach -t ofm_linux_host_sync')
    print(f'Or follow the log: ssh -t {shlex.quote(target)} sudo tail -f {shlex.quote(log_file)}')


def run_linux_host_sync_baked(c: Connection, hostname: str, *, force_download: bool) -> None:
    """Run one bake sync phase in a detached tmux session, then wait for it to finish.

    The golden-AMI bake (``deploy --bake``) needs the opposite of the two existing runners.
    ``run_linux_host_sync_detached`` survives an SSH drop (the planet download runs for hours)
    but returns immediately and never surfaces the result, so a bake could not tell the phases
    apart or fail on a bad download. A plain foreground ``c.sudo`` surfaces the result but dies
    with the SSH connection -- a dropped laptop link orphaned a half-finished download on the
    box with no way to resume cleanly.

    This runner gets both: the sync runs **inside a detached tmux session** (so it keeps going
    across a disconnect), while the deploy **polls** for completion and reads an exit-code
    sentinel the tmux command writes when the sync returns. If the operator's connection drops
    during the long planet download, the remote sync keeps running to completion in tmux -- the
    operator reconnects and watches it with the printed ``attach``/``tail`` commands rather than
    re-running the deploy (a re-run would ``clean_linux_host`` the box, killing the live sync and
    discarding the in-progress area; already-finished areas' ``tiles.btrfs`` images survive that
    clean, so only an unfinished area is lost).

    ``force_download`` passes ``--download`` so the bake downloads tiles even though the baked
    config is ``local_versions: true``. Output is tee'd to the same ``sync.log`` as the other
    runners; the sentinel lives beside it so a stale one from a previous phase is overwritten.
    """
    log_file = f'{linux_host_deploy_config.remote_linux_host_dir}/logs/sync.log'
    status_file = f'{linux_host_deploy_config.remote_linux_host_dir}/logs/bake_sync.status'
    session = 'ofm_linux_host_bake'
    download_flag = ' --download' if force_download else ''
    phase = 'download' if force_download else 'local-serve'

    inner = (
        f'cd {linux_host_deploy_config.remote_source_dir} && '
        f'env PYTHONUNBUFFERED=1 ./linux_host/scripts/linux_host.py sync{download_flag}'
    )
    # pipefail so the sync's exit status (not tee's) is what reaches $?. The sentinel is written
    # unconditionally after the pipeline so the poller can distinguish "still running" (no file)
    # from "finished" (file holds the exit code) -- including a non-zero failure.
    tmux_command = (
        f'set -o pipefail; {inner} 2>&1 | tee -a {shlex.quote(log_file)}; '
        f'echo $? > {shlex.quote(status_file)}'
    )
    # Clear any sentinel from an earlier phase/run, then launch detached. A session left over
    # from a crashed run is killed first so the new phase starts clean.
    c.sudo(f'rm -f {shlex.quote(status_file)}')
    c.sudo(f'tmux kill-session -t {session} 2>/dev/null; true')
    c.sudo(f'tmux new-session -d -s {session} bash -c {shlex.quote(tmux_command)}')

    target = f'{c.user}@{hostname}' if c.user else hostname
    print(f'Bake {phase} sync running detached in tmux on {hostname}; waiting for it to finish.')
    print(f'  Attach:     ssh -t {shlex.quote(target)} sudo tmux attach -t {session}')
    print(f'  Follow log: ssh -t {shlex.quote(target)} sudo tail -f {shlex.quote(log_file)}')

    # Poll the sentinel. The download phase can run for hours; a dropped connection here does not
    # kill the remote sync (it is in tmux), so it runs on to completion -- reconnect and attach
    # rather than re-running the bake (see the docstring).
    while True:
        time.sleep(15)
        result = c.sudo(f'cat {shlex.quote(status_file)} 2>/dev/null || true', hide=True)
        raw = result.stdout.strip()
        if not raw:
            continue
        code = int(raw)
        if code != 0:
            raise RuntimeError(
                f'bake {phase} sync failed on {hostname} (exit {code}). '
                f'See {log_file} on the host (attach command printed above).'
            )
        print(f'Bake {phase} sync finished on {hostname}.')
        return


def install_linux_host_cron(c: Connection) -> None:
    put(
        c,
        linux_host_deploy_config.local_linux_host_dir / 'cron.d' / 'ofm_linux_host',
        '/etc/cron.d/',
    )
