import json
import shlex
from pathlib import Path

from fabric import Connection

from linux_host.deploy_linux_host.linux_host_deploy_config import linux_host_deploy_config
from linux_host.deploy_linux_host.nginx import configure_nginx
from linux_host.linux_host_lib.config_loader import (
    read_linux_host_jsonc_config,
    resolve_upload_cert_paths,
)
from shared_lib.ssh_lib.dnf import dnf_install
from shared_lib.ssh_lib.kernel import kernel_limits1m, kernel_somaxconn65k
from shared_lib.ssh_lib.utils import append_str, get_username, put, run_nice


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
    c.sudo('rm -rf /mnt/ofm')
    c.sudo('mkdir -p /mnt/ofm')

    versions_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/versions'
    c.sudo(f'mkdir -p {versions_dir}')
    keep_areas = ' '.join(f'! -name {shlex.quote(area)}' for area in areas)
    c.sudo(f'find {versions_dir} -mindepth 1 -maxdepth 1 {keep_areas} -exec rm -rf -- {{}} +')
    c.sudo(f'rm -rf {linux_host_deploy_config.remote_linux_host_dir}/runs')
    # The download staging dir may be a mounted ephemeral NVMe (mount_nvme_download_volume):
    # emptying its contents but keeping the mountpoint avoids a "device busy" failure on rm and
    # preserves the NVMe mount across a redeploy. If it is a plain dir, remove it outright.
    tmp_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/tmp'
    clean_tmp = (
        f'if mountpoint -q {tmp_dir}; then find {tmp_dir} -mindepth 1 -delete; '
        f'else rm -rf {tmp_dir}; fi'
    )
    c.sudo(f'bash -c {shlex.quote(clean_tmp)}')

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
    kernel_somaxconn65k(c)
    kernel_limits1m(c)
    configure_nginx(c)
    install_tile_auth_service(c)
    # No host firewall on AL2023: inbound access is controlled by the AWS security group
    # (port 80 from the ALB, optionally 443 for non-ALB TLS cert types). Upstream's `ufw`
    # calls do not apply here since AL2023 does not ship ufw.

    c.sudo(f'mkdir -p {linux_host_deploy_config.remote_linux_host_dir}/logs')
    c.sudo(f'chown ofm:ofm {linux_host_deploy_config.remote_linux_host_dir}/logs')

    nginx_logs_dir = f'{linux_host_deploy_config.remote_linux_host_dir}/logs_nginx'
    c.sudo(f'mkdir -p {nginx_logs_dir}')
    c.sudo(f'chown nginx:nginx {nginx_logs_dir}')

    jsonc_data = read_linux_host_jsonc_config(jsonc_path)
    nginx_log_paths = ['/data/nginx/logs/nginx-error.log']
    for domain_data in jsonc_data['domains']:
        base_path = f'{nginx_logs_dir}/{domain_data["slug"]}'
        nginx_log_paths.extend(
            [f'{base_path}-access.jsonl', f'{base_path}-error.log', f'{base_path}-deny.log']
        )
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


def mount_nvme_download_volume(c: Connection, *, min_size_gb: int = 100) -> None:
    """Put the (throwaway) compressed btrfs download on a local ephemeral NVMe, if available.

    If an unformatted NVMe disk large enough to hold the ~90 GB gzipped planet download is
    present, it is partitioned (GPT, one partition), formatted (ext4) and mounted at the download
    staging dir (``/data/ofm/linux_host/tmp``). The ``.gz`` then lands on fast, ephemeral local
    NVMe and is stream-decompressed straight onto the versions volume (see ``prepare_version`` in
    linux_host_lib/btrfs.py), so the extracted ``tiles.btrfs`` -- the thing an AMI should capture
    -- stays on EBS while only the disposable ``.gz`` bytes live on the NVMe. Instance-store NVMe
    is never part of an AMI, so the download data is automatically excluded from any image.

    Only *unformatted* disks are considered: no filesystem, no partitions, not mounted. Existing
    data is therefore never touched. This matches instance-store NVMe (e.g. the 118 GB volume on
    an ``m6gd.large``) and any extra blank NVMe-attached volume.

    Instance-store NVMe is ephemeral (wiped on stop/start), so the fstab entry uses ``nofail`` to
    never block boot if the volume comes back blank or is gone; the download then simply falls
    back to the versions (EBS) volume.
    """
    download_tmp = f'{linux_host_deploy_config.remote_linux_host_dir}/tmp'
    print(f'Looking for an unformatted NVMe volume for the btrfs download ({download_tmp})')

    c.sudo(f'mkdir -p {download_tmp}')
    if c.sudo(f'mountpoint -q {download_tmp}', warn=True, hide=True).ok:
        print(f'  {download_tmp} is already a mount point, skipping')
        return

    out = c.run('lsblk -b -J -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT', hide=True).stdout
    devices = json.loads(out)['blockdevices']

    min_bytes = min_size_gb * 1000**3
    candidates = []
    for dev in devices:
        if dev.get('type') != 'disk':
            continue
        if not dev['name'].startswith('nvme'):
            continue
        # skip anything already carrying a filesystem, partitions or a mount
        if dev.get('fstype') or dev.get('mountpoint') or dev.get('children'):
            continue
        if int(dev.get('size') or 0) < min_bytes:
            continue
        candidates.append(dev)

    if not candidates:
        print(
            f'  no unformatted NVMe disk >= {min_size_gb} GB found, '
            f'downloading onto the versions volume'
        )
        return

    # pick the largest suitable disk
    dev = max(candidates, key=lambda d: int(d['size']))
    devpath = f'/dev/{dev["name"]}'
    print(f'  using {devpath} ({int(dev["size"]) / 1000**3:.0f} GB) for the btrfs download')

    # parted is not guaranteed on a bare image, and this may run before pkg_base
    dnf_install(c, 'parted', warn=True)

    # GPT label + a single partition spanning the whole disk
    c.sudo(f'parted -s {devpath} mklabel gpt')
    c.sudo(f'parted -s -a optimal {devpath} mkpart primary ext4 0% 100%')
    c.sudo('udevadm settle', warn=True)

    # NVMe partition node: nvme1n1 -> nvme1n1p1
    partpath = f'{devpath}p1'

    # -m 0: don't reserve 5% for root, we only store the throwaway .gz here
    c.sudo(f'mkfs.ext4 -F -m 0 {partpath}')

    uuid = c.sudo(f'blkid -s UUID -o value {partpath}', hide=True).stdout.strip()

    c.sudo(f'mkdir -p {download_tmp}')
    fstab_line = f'UUID={uuid} {download_tmp} ext4 defaults,nofail 0 2'
    append_str(c, '/etc/fstab', fstab_line, check_duplicate=True)
    c.sudo(f'mount {download_tmp}')


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


def run_linux_host_sync_detached(c: Connection, hostname: str) -> None:
    command = (
        f'cd {linux_host_deploy_config.remote_source_dir} && '
        'env PYTHONUNBUFFERED=1 ./linux_host/scripts/linux_host.py sync'
    )
    c.sudo(f'tmux new-session -d -s ofm_linux_host_sync {shlex.quote(command)}')
    print(f'Attach with: ssh -t {shlex.quote(hostname)} sudo tmux attach -t ofm_linux_host_sync')


def install_linux_host_cron(c: Connection) -> None:
    put(
        c,
        linux_host_deploy_config.local_linux_host_dir / 'cron.d' / 'ofm_linux_host',
        '/etc/cron.d/',
    )
