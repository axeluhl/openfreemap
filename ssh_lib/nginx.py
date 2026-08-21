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

    c.sudo('nginx -t')
    c.sudo('systemctl enable nginx')
    c.sudo('systemctl restart nginx')

