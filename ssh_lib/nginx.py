from ssh_lib import ASSETS_DIR
from ssh_lib.utils import (
    exists,
    pkg_install,
    put,
    put_str,
)


def nginx(c):
    if not exists(c, '/usr/sbin/nginx'):
        # nginx mainline RPM repo for Amazon Linux 2023
        put_str(
            c,
            '/etc/yum.repos.d/nginx.repo',
            '[nginx-mainline]\n'
            'name=nginx mainline repo\n'
            'baseurl=http://nginx.org/packages/mainline/amzn/2023/$basearch/\n'
            'gpgcheck=1\n'
            'enabled=1\n'
            'gpgkey=https://nginx.org/keys/nginx_signing.key\n'
            'module_hotfixes=true\n',
        )
        pkg_install(c, 'nginx')

    c.sudo('rm -rf /data/nginx/config')
    c.sudo('mkdir -p /data/nginx/config')

    c.sudo('rm -rf /data/nginx/logs')
    c.sudo('mkdir -p /data/nginx/logs')

    c.sudo('mkdir -p /data/nginx/sites')

    # TLS is terminated upstream (e.g. an AWS ALB); nginx serves plain HTTP
    # on port 80 only, so no certificates are generated or configured here.
    # The generated ofm_direct.conf vhost is the default_server, so a separate
    # default_disable server is not uploaded.

    put(c, f'{ASSETS_DIR}/nginx/nginx.conf', '/etc/nginx/')
    put(c, f'{ASSETS_DIR}/nginx/mime.types', '/etc/nginx/')
    put(c, f'{ASSETS_DIR}/nginx/cloudflare.conf', '/data/nginx/config')

    # Remove any stale nginx dependency drop-in from earlier versions that made nginx.service
    # hard-require ofm-tile-auth.service. That Requires= could block nginx from (re)starting if
    # the tile-auth unit failed (e.g. before tiles are mounted during a deploy). Ordering is now
    # expressed solely by ofm-tile-auth.service's Before=nginx.service.
    c.sudo('rm -f /etc/systemd/system/nginx.service.d/ofm-tile-auth.conf')
    c.sudo('systemctl daemon-reload')

    c.sudo('nginx -t')
    c.sudo('systemctl enable nginx')
    c.sudo('systemctl restart nginx')


def install_tile_auth_service(c):
    """Install & enable the boot-time EC2-user-data tile-auth ingestion service.

    Baked into the AMI so that each instance launched from it applies a TILE_AUTH_SECRETS
    handed in via EC2 user data before nginx starts (and no secret is baked into the image).
    The ordering is expressed entirely by the unit's ``Before=nginx.service`` plus
    ``WantedBy=multi-user.target``; there is deliberately no hard ``Requires=`` drop-in on
    nginx, so a deploy-time or manual ``systemctl restart nginx`` is never blocked by this unit.

    Must run *after* upload_http_host_files (the unit's ExecStart runs
    /data/ofm/http_host/bin/http_host.py). We only enable it (it runs on the next boot); we do
    not start it during the deploy, so the deploy host's own user data is never read here.
    """
    put(
        c,
        f'{ASSETS_DIR}/systemd/ofm-tile-auth.service',
        '/etc/systemd/system/ofm-tile-auth.service',
    )
    # Remove any stale hard-Requires drop-in from earlier versions (also cleaned in nginx()).
    c.sudo('rm -f /etc/systemd/system/nginx.service.d/ofm-tile-auth.conf')
    c.sudo('systemctl daemon-reload')
    c.sudo('systemctl enable ofm-tile-auth.service')

