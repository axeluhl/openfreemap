from fabric import Connection

from .dnf import (
    dnf_install,
    dnf_update,
)


def pkg_base(c: Connection) -> None:
    # Packages actually required for OFM to work on Amazon Linux 2023.
    # A clean AL2023 image already ships curl, ca-certificates, tar, gzip,
    # util-linux, findutils and python3, so those are intentionally omitted.
    # AL2023's default `python3` is 3.9, but OFM requires >=3.10. uv (installed
    # separately via ensure_uv) manages its own Python toolchain, so the venv
    # flow does not depend on the system interpreter being >=3.10. A newer
    # system interpreter (python3.11, from the AL2023 base repos) plus its build
    # headers are still installed so native builds such as `pycurl` keep working.
    required = [
        # unpacking / serving tiles
        'pigz',  # unpigz for tiles.btrfs.gz
        'unzip',
        'wget',  # also the download fallback when aria2 is absent
        'git',
        'python3.11',
        'python3.11-pip',
        # build deps for native wheels such as `pycurl` (used by the host)
        'gcc',
        'python3.11-devel',
        'libcurl-devel',
        'openssl-devel',
    ]

    dnf_install(c, ' '.join(required))

    # Optional quality-of-life / monitoring tools plus aria2. Best-effort only:
    # install what AL2023's repos provide and silently skip the rest.
    #
    # NOTE on btrfs / aria2 for the http host:
    #   * btrfs-progs is NOT needed to serve tiles: the AL2023 kernel mounts the
    #     tiles.btrfs image read-only on its own (mount -a). The btrfs userspace
    #     tools are only used by tile generation (mkfs/balance/resize).
    #   * aria2 is only a download accelerator; the download helper falls back
    #     to wget when aria2c is not installed, so it is optional here.
    #   Both live in EPEL/SPAL, not the AL2023 base repos.
    optional = [
        'aria2',
        'htop',
        'pv',  # live progress bar for the sync's checksum / decompress phases
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

    dnf_install(c, ' '.join(optional), warn=True, skip_broken=True)


def pkg_upgrade(c: Connection) -> None:
    dnf_update(c)
    c.sudo('dnf upgrade -y')
