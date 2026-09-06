from pathlib import Path

from fabric import Connection

from .dnf import add_dnf_repo, dnf_install
from .utils import exists, put


NGINX_REPO_NAME = 'nginx'


def deploy_nginx_base_config(c: Connection, assets_dir: str | Path, alb_mode: bool = False) -> None:
    update_nginx_packages(c)

    c.sudo('mkdir -p /data/nginx/config /data/nginx/logs /data/nginx/sites')

    assets_dir = Path(assets_dir)
    put(c, assets_dir / 'nginx.conf', '/etc/nginx/nginx.conf')
    put(c, assets_dir / 'mime.types', '/etc/nginx/mime.types')
    if alb_mode:
        # An all-ALB host lets its first tile vhost own the port-80 default_server (so it
        # answers ALB health checks that arrive by IP). default_disable would then be a second
        # port-80 default_server -> nginx fatal; remove any copy left by an earlier deploy.
        c.sudo('rm -f /data/nginx/sites/default_disable.conf')
    else:
        put(c, assets_dir / 'default_disable.conf', '/data/nginx/sites/default_disable.conf')
    put(c, assets_dir / 'cloudflare.conf', '/data/nginx/config/cloudflare.conf')

    c.sudo('nginx -t')
    c.sudo('systemctl enable nginx')
    c.sudo('systemctl restart nginx')


def update_nginx_packages(c: Connection) -> None:
    # Amazon Linux 2023 serves plain HTTP on :80 behind an AWS ALB that
    # terminates TLS, so the upstream ACME/Let's Encrypt nginx module
    # (nginx-module-acme, only available from nginx's own apt repo) is NOT
    # required here. Install stock nginx from the nginx mainline RPM repo for
    # AL2023 via dnf instead of building a custom nginx with the ACME module.
    if exists(c, '/usr/sbin/nginx'):
        return

    add_dnf_repo(
        c,
        NGINX_REPO_NAME,
        '[nginx-mainline]\n'
        'name=nginx mainline repo\n'
        'baseurl=http://nginx.org/packages/mainline/amzn/2023/$basearch/\n'
        'gpgcheck=1\n'
        'enabled=1\n'
        'gpgkey=https://nginx.org/keys/nginx_signing.key\n'
        'module_hotfixes=true\n',
    )

    dnf_install(c, 'nginx')


def ensure_self_signed_cert(c: Connection) -> None:
    # Kept for callers that still terminate TLS on the host. The AL2023 ALB
    # target does not use this: TLS is terminated upstream and default_disable
    # rejects unknown SNI with `ssl_reject_handshake`, which needs no cert.
    if exists(c, '/etc/nginx/ssl/self_signed.cert'):
        return

    c.sudo('mkdir -p /etc/nginx/ssl')
    c.sudo(
        'openssl req -x509 -nodes -days 3650 -newkey rsa:2048 '
        + '-keyout /etc/nginx/ssl/self_signed.key -out /etc/nginx/ssl/self_signed.cert '
        + '-subj "/C=US/ST=Dummy/L=Dummy/O=Dummy/CN=example.com"',
        hide=True,
    )
