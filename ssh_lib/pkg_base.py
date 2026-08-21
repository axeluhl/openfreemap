from ssh_lib.utils import (
    pkg_install,
    pkg_update,
)


def pkg_base(c):
    # Packages actually required for OFM to work on Amazon Linux 2023.
    # A clean AL2023 image already ships curl, ca-certificates, tar, gzip,
    # util-linux, findutils and python3, so those are intentionally omitted.
    required = [
        # downloading / unpacking / mounting tiles
        'aria2',  # download_file_aria2()
        'pigz',  # unpigz for tiles.btrfs.gz
        'btrfs-progs',  # mounting the btrfs image (mkfs for tile_gen)
        'unzip',
        'wget',
        'git',  # repo + planetiler clone (tile_gen)
        'python3-pip',
        # build deps for `pip install pycurl` (used by tile_gen/http_host)
        'gcc',
        'python3-devel',
        'libcurl-devel',
        'openssl-devel',
    ]

    pkg_install(c, ' '.join(required))

    # Optional quality-of-life / monitoring tools. Best-effort only: install
    # what AL2023's repos provide and silently skip the rest.
    optional = [
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
