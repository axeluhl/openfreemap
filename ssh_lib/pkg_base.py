from ssh_lib.utils import (
    pkg_install,
    pkg_update,
)


def pkg_base(c):
    # Packages actually required for OFM to work on Amazon Linux 2023.
    # A clean AL2023 image already ships curl, ca-certificates, tar, gzip,
    # util-linux, findutils and python3, so those are intentionally omitted.
    # AL2023's default `python3` is 3.9, but OFM requires >=3.10. Install a
    # newer interpreter (python3.11, available in the AL2023 base repos) and
    # build the /data/ofm venv from it (see modules/prepare-virtualenv.sh).
    required = [
        # unpacking / serving tiles
        'pigz',  # unpigz for tiles.btrfs.gz
        'unzip',
        'wget',  # also the download fallback when aria2 is absent
        'git',
        'python3.11',
        'python3.11-pip',
        # build deps for `pip install pycurl` (used by http_host)
        'gcc',
        'python3.11-devel',
        'libcurl-devel',
        'openssl-devel',
    ]

    pkg_install(c, ' '.join(required))

    # Optional quality-of-life / monitoring tools plus aria2. Best-effort only:
    # install what AL2023's repos provide and silently skip the rest.
    #
    # NOTE on btrfs / aria2 for http-host:
    #   * btrfs-progs is NOT needed to serve tiles: the AL2023 kernel mounts the
    #     tiles.btrfs image read-only on its own (mount -a). The btrfs userspace
    #     tools are only used by tile_gen (mkfs/balance/resize).
    #   * aria2 is only a download accelerator; download_file_aria2() falls back
    #     to wget when aria2c is not installed, so it is optional here.
    #   Both live in EPEL/SPAL, not the AL2023 base repos.
    optional = [
        'aria2',
        'htop',
        'tmux',
        'mc',
        'ncdu',
        'nano',
        'lsof',
        'rsync',
        'file',
        'bind-utils',  # dig / nslookup
        'net-tools',
        'man-db',
        'bash-completion',
        'vnstat',
    ]

    pkg_install(c, ' '.join(optional), warn=True, skip_broken=True)


def pkg_upgrade(c):
    pkg_update(c)
    c.sudo('dnf upgrade -y')
