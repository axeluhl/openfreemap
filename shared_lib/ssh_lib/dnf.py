from fabric import Connection

from .utils import put_str


def dnf_update(c: Connection) -> None:
    # dnf refreshes metadata automatically; makecache keeps the behaviour explicit.
    c.sudo('dnf -y makecache', warn=True)


def dnf_install(c: Connection, pkgs: str, warn: bool = False, skip_broken: bool = False) -> None:
    skip = '--skip-broken ' if skip_broken else ''
    c.sudo(
        f'dnf install -y {skip}{pkgs}',
        warn=warn,
        echo=True,
    )


def dnf_remove(c: Connection, pkgs: str) -> None:
    c.sudo(f'dnf remove -y {pkgs}', warn=True)


def dnf_autoremove(c: Connection) -> None:
    c.sudo('dnf autoremove -y')


def add_dnf_repo(c: Connection, repo_name: str, repo_content: str) -> None:
    """
    Drop a third-party dnf/yum repository definition into /etc/yum.repos.d/.

    Amazon Linux 2023 is RHEL-family, so third-party repos ship as a single
    ``.repo`` file with an inline ``gpgkey`` URL (no separate keyring dance like
    apt needs). ``repo_content`` is the full INI body.
    """
    put_str(c, f'/etc/yum.repos.d/{repo_name}.repo', repo_content)
