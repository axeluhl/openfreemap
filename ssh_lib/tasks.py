import json
import sys

from ssh_lib import (
    CONFIG_DIR,
    HTTP_HOST_BIN,
    HTTP_HOST_RUNS,
    MODULES_DIR,
    OFM_DIR,
    REMOTE_CONFIG,
    TILE_GEN_BIN,
    VENV_BIN,
    dotenv_val,
)
from ssh_lib.benchmark import c1000k, wrk
from ssh_lib.kernel import kernel_limits1m, kernel_somaxconn65k
from ssh_lib.nginx import nginx
from ssh_lib.pkg_base import pkg_base, pkg_upgrade
from ssh_lib.planetiler import install_planetiler
from ssh_lib.rclone import rclone
from ssh_lib.utils import (
    add_user,
    append_str,
    enable_sudo,
    get_username,
    put,
    put_dir,
    put_str,
    run_nice,
    sudo_cmd,
)


def mount_nvme_data_volume(c, *, min_size_gb=200):
    """
    If an unformatted NVMe disk large enough to hold the uncompressed btrfs
    image is present, format it (ext4) and mount it at /data/ofm, so the
    (multi-hundred-GB) tiles.btrfs lives on fast local NVMe instead of the root
    disk.

    Only *unformatted* disks are considered: no filesystem, no partitions, not
    mounted. Existing data is therefore never touched. This matches both an
    extra unformatted EBS volume and instance-store NVMe.

    Instance-store NVMe is ephemeral (wiped on stop/start), so the fstab entry
    uses `nofail` to never block boot if the volume comes back blank or is gone.
    """
    print('Looking for an unformatted NVMe volume for /data/ofm')

    if c.sudo('mountpoint -q /data/ofm', warn=True, hide=True).ok:
        print('  /data/ofm is already a mount point, skipping')
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
        print(f'  no unformatted NVMe disk >= {min_size_gb} GB found, using root disk')
        return

    # pick the largest suitable disk
    dev = max(candidates, key=lambda d: int(d['size']))
    devpath = f"/dev/{dev['name']}"
    print(f"  using {devpath} ({int(dev['size']) / 1000**3:.0f} GB) for /data/ofm")

    # -m 0: don't reserve 5% for root, we only store the big image file here
    c.sudo(f'mkfs.ext4 -F -m 0 {devpath}')

    uuid = c.sudo(f'blkid -s UUID -o value {devpath}', hide=True).stdout.strip()

    c.sudo('mkdir -p /data/ofm')

    fstab_line = f'UUID={uuid} /data/ofm ext4 defaults,nofail 0 2'
    append_str(c, '/etc/fstab', fstab_line, check_duplicate=True)

    c.sudo('mount /data/ofm')


def prepare_shared(c, domain=None):
    # creates ofm user with uid=2000, disabled password and nopasswd sudo
    add_user(c, 'ofm', uid=2000)
    enable_sudo(c, 'ofm', nopasswd=True)

    pkg_upgrade(c)
    pkg_base(c)
    rclone(c)

    c.sudo(f'mkdir -p {REMOTE_CONFIG}')
    c.sudo(f'chown ofm:ofm {REMOTE_CONFIG}')
    c.sudo(f'chown ofm:ofm {OFM_DIR}')

    upload_config_json(c, domain=domain)

    prepare_venv(c)


def prepare_venv(c):
    put(
        c,
        MODULES_DIR / 'prepare-virtualenv.sh',
        OFM_DIR,
        permissions='755',
        user='ofm',
    )
    sudo_cmd(c, f'cd {OFM_DIR} && source prepare-virtualenv.sh')


def prepare_tile_gen(c, *, enable_cron):
    c.sudo('rm -f /etc/cron.d/ofm_tile_gen')

    install_planetiler(c)

    c.sudo(f'rm -rf {TILE_GEN_BIN}')

    put_dir(c, MODULES_DIR / 'tile_gen', TILE_GEN_BIN, file_permissions='755')

    for dirname in ['tile_gen_lib', 'scripts']:
        put_dir(c, MODULES_DIR / 'tile_gen' / dirname, f'{TILE_GEN_BIN}/{dirname}')

    if (CONFIG_DIR / 'rclone.conf').exists():
        put(
            c,
            CONFIG_DIR / 'rclone.conf',
            f'{REMOTE_CONFIG}/rclone.conf',
            permissions='600',
            user='ofm',
        )

    c.sudo(f'{VENV_BIN}/pip install -e {TILE_GEN_BIN} --use-pep517')

    c.sudo('rm -rf /data/ofm/tile_gen/logs')
    c.sudo('mkdir -p /data/ofm/tile_gen/logs')

    c.sudo('chown ofm:ofm /data/ofm/tile_gen/{,*}')
    c.sudo(f'chown ofm:ofm -R {TILE_GEN_BIN}')

    if enable_cron:
        put(c, MODULES_DIR / 'tile_gen' / 'cron.d' / 'ofm_tile_gen', '/etc/cron.d/')


def prepare_http_host(c):
    kernel_somaxconn65k(c)
    kernel_limits1m(c)

    nginx(c)

    c.sudo('rm -rf /data/ofm/http_host/logs')
    c.sudo('mkdir -p /data/ofm/http_host/logs')
    c.sudo('chown ofm:ofm /data/ofm/http_host/logs')

    c.sudo('rm -rf /data/ofm/http_host/logs_nginx')
    c.sudo('mkdir -p /data/ofm/http_host/logs_nginx')
    c.sudo('chown nginx:nginx /data/ofm/http_host/logs_nginx')

    upload_http_host_files(c)

    c.sudo(f'{VENV_BIN}/pip install -e {HTTP_HOST_BIN} --use-pep517')


def run_http_host_sync(c):
    print('Running http_host.py sync --force')
    sudo_cmd(c, f'{VENV_BIN}/python -u {HTTP_HOST_BIN}/http_host.py sync --force')


def copy_runs_from_host(c, src_host, src_user=None):
    """
    Copies the btrfs run(s) for all areas from an already-provisioned host via
    scp, instead of downloading the (multi-hundred-GB) tiles.btrfs.gz files from
    the web.

    On the source host the runs are expected under
    /data/ofm/http_host/runs/<area>/<version>/ (tiles.btrfs plus the metadata
    json). They are copied into the same location on the target host, so the
    subsequent `http_host.py sync` finds tiles.btrfs already present and skips
    the download for those versions. Planet is the big one; the other areas are
    tiny and copied along with it.

    src_user defaults to the login user used to connect to the target host.
    The scp runs as that login user, so SSH auth to src_host uses its forwarded
    ssh-agent (agent forwarding is enabled on the connection in init-server.py).
    """
    login_user = get_username(c)
    src_user = src_user or login_user

    staging = f'{HTTP_HOST_RUNS}/_copy_tmp'

    print(f'Copying btrfs runs from {src_user}@{src_host}:{HTTP_HOST_RUNS}')

    # /data/ofm is owned by ofm, so create a staging dir the (non-ofm) login
    # user running scp is allowed to write into
    c.sudo(f'mkdir -p {HTTP_HOST_RUNS}')
    c.sudo(f'rm -rf {staging}')
    c.sudo(f'mkdir -p {staging}')
    c.sudo(f'chown {login_user} {staging}')

    # copy every area dir from the source runs dir into the staging dir;
    # trailing /. copies the contents (the <area> subdirs), not the runs dir
    run_nice(
        c,
        f'scp -rp -o StrictHostKeyChecking=accept-new '
        f'{src_user}@{src_host}:{HTTP_HOST_RUNS}/. {staging}/',
    )
    # the source may itself contain a leftover staging dir; drop it
    c.sudo(f'rm -rf {staging}/_copy_tmp')

    # move each area into place (same filesystem -> instant) and normalise
    # ownership to root, matching files created by the regular download path.
    # NOTE: iterate in Python, not a remote shell loop -- sudo_cmd wraps
    # commands in `bash -c "..."`, so the outer shell would expand $var / $(...)
    # before the inner bash runs.
    area_names = c.sudo(f'ls -1 {staging}', hide=True).stdout.split()
    for name in area_names:
        c.sudo(f'rm -rf {HTTP_HOST_RUNS}/{name}')
        c.sudo(f'mv {staging}/{name} {HTTP_HOST_RUNS}/{name}')
    c.sudo(f'chown -R root:root {HTTP_HOST_RUNS}')
    c.sudo(f'rm -rf {staging}')


def upload_http_host_files(c):
    c.sudo(f'rm -rf {HTTP_HOST_BIN}')
    c.sudo(f'mkdir -p {HTTP_HOST_BIN}')

    put_dir(c, MODULES_DIR / 'http_host', HTTP_HOST_BIN, file_permissions='755')

    for dirname in ['http_host_lib', 'scripts']:
        put_dir(c, MODULES_DIR / 'http_host' / dirname, f'{HTTP_HOST_BIN}/{dirname}')

    put_dir(
        c,
        MODULES_DIR / 'http_host' / 'http_host_lib' / 'nginx_confs',
        f'{HTTP_HOST_BIN}/http_host_lib/nginx_confs',
    )

    c.sudo('chown -R ofm:ofm /data/ofm/http_host')


def install_benchmark(c):
    """
    Read docs/quick_notes/http_benchmark.md
    """
    c1000k(c)
    wrk(c)


def upload_config_json(c, domain=None):
    # The nginx server_name / public hostname can be passed on the command line
    # (--domain); otherwise it falls back to DOMAIN_DIRECT in config/.env.
    domain_direct = (domain or dotenv_val('DOMAIN_DIRECT')).lower()
    skip_planet = dotenv_val('SKIP_PLANET').lower() == 'true'

    # TLS is terminated upstream (e.g. an AWS ALB), so no certificate settings
    # are needed here. domain_direct is the public hostname used in server_name
    # and in the generated TileJSON/style URLs.
    if not domain_direct:
        sys.exit('Please specify a domain via --domain or DOMAIN_DIRECT in config/.env')

    http_host_list = [h.strip() for h in dotenv_val('HTTP_HOST_LIST').split(',') if h.strip()]

    config = {
        'domain_direct': domain_direct,
        # kept as an empty passthrough for the optional loadbalancer/tile_gen modules
        'domain_roundrobin': dotenv_val('DOMAIN_ROUNDROBIN').lower(),
        'skip_planet': skip_planet,
        'http_host_list': http_host_list,
        'telegram_token': dotenv_val('TELEGRAM_TOKEN'),
        'telegram_chat_id': dotenv_val('TELEGRAM_CHAT_ID'),
    }

    config_str = json.dumps(config, indent=2, ensure_ascii=False)
    print(config_str)
    put_str(c, f'{REMOTE_CONFIG}/config.json', config_str)


def setup_loadbalancer(c):
    c.sudo('rm -f /etc/cron.d/ofm_loadbalancer')

    put(
        c,
        CONFIG_DIR / 'cloudflare.ini',
        f'{REMOTE_CONFIG}/cloudflare.ini',
        permissions=400,
    )

    c.sudo('rm -rf /data/ofm/loadbalancer')
    put_dir(c, MODULES_DIR / 'loadbalancer', '/data/ofm/loadbalancer')
    put_dir(
        c,
        MODULES_DIR / 'loadbalancer' / 'loadbalancer_lib',
        '/data/ofm/loadbalancer/loadbalancer_lib',
    )

    c.sudo(f'{VENV_BIN}/pip install -e /data/ofm/loadbalancer --use-pep517')

    c.sudo('mkdir -p /data/ofm/loadbalancer/logs')
    c.sudo('chown -R ofm:ofm /data/ofm/loadbalancer')

    put(c, MODULES_DIR / 'loadbalancer' / 'cron.d' / 'ofm_loadbalancer', '/etc/cron.d/')
