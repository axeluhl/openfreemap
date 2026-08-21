from ssh_lib import ASSETS_DIR
from ssh_lib.utils import (
    exists,
    get_latest_release_github,
    pkg_install,
    put,
    put_str,
    sudo_cmd,
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
    c.sudo('mkdir -p /data/nginx/acme-challenges')
    c.sudo('mkdir -p /data/nginx/certs')

    generate_self_signed_cert(c)

    put(c, f'{ASSETS_DIR}/nginx/nginx.conf', '/etc/nginx/')
    put(c, f'{ASSETS_DIR}/nginx/mime.types', '/etc/nginx/')
    put(c, f'{ASSETS_DIR}/nginx/default_disable.conf', '/data/nginx/sites')
    put(c, f'{ASSETS_DIR}/nginx/cloudflare.conf', '/data/nginx/config')

    sudo_cmd(c, 'curl https://ssl-config.mozilla.org/ffdhe2048.txt -o /etc/nginx/ffdhe2048.txt')

    c.sudo('nginx -t')
    c.sudo('systemctl enable nginx')
    c.sudo('systemctl restart nginx')


def certbot(c):
    # snapd is not available on Amazon Linux 2023, so use certbot's official
    # pip-in-a-venv install method into /opt/certbot.
    pkg_install(c, 'python3 python3-pip augeas-libs')

    if not exists(c, '/opt/certbot/bin/certbot'):
        c.sudo('python3 -m venv /opt/certbot')

    c.sudo('/opt/certbot/bin/pip install --upgrade pip', echo=True)
    c.sudo('/opt/certbot/bin/pip install certbot certbot-dns-cloudflare', echo=True)
    c.sudo('ln -snf /opt/certbot/bin/certbot /usr/local/bin/certbot')


def lego(c):
    lego_version = get_latest_release_github('go-acme', 'lego')

    url = f'https://github.com/go-acme/lego/releases/download/{lego_version}/lego_{lego_version}_linux_amd64.tar.gz'

    c.sudo('rm -rf /tmp/lego*')
    c.sudo('mkdir -p /tmp/lego')
    c.sudo(
        f'wget -q "{url}" -O /tmp/lego/out.tar.gz',
    )
    c.sudo('tar xzvf /tmp/lego/out.tar.gz -C /tmp/lego')
    c.sudo('chmod +x /tmp/lego/lego')
    c.sudo('mv /tmp/lego/lego /usr/local/bin')
    c.sudo('rm -rf /tmp/lego*')


def generate_self_signed_cert(c):
    if exists(c, '/etc/nginx/ssl/dummy.cert'):
        return

    c.sudo('mkdir -p /etc/nginx/ssl')
    c.sudo(
        'openssl req -x509 -nodes -days 3650 -newkey rsa:2048 '
        '-keyout /etc/nginx/ssl/dummy.key -out /etc/nginx/ssl/dummy.cert '
        '-subj "/C=US/ST=Dummy/L=Dummy/O=Dummy/CN=example.com"',
        hide=True,
    )
