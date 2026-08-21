from ssh_lib import MODULES_DIR
from ssh_lib.utils import exists, pkg_install, put


def c1000k(c):
    if exists(c, 'c1000k-master'):
        return

    c.run('wget https://github.com/ideawu/c1000k/archive/master.zip -O tmp.zip')
    c.run('unzip -o tmp.zip')
    c.run('rm tmp.zip')
    c.run('cd c1000k-master && make')

    # usage
    # ./server 7000
    # ./client 127.0.0.1 7000
    # make sure it runs till 1 million


def wrk(c):
    pkg_install(c, 'wrk', warn=True, skip_broken=True)
    c.sudo('mkdir -p /data/ofm/benchmark')
    put(c, f'{MODULES_DIR}/http_host/benchmark/wrk_custom_list.lua', '/data/ofm/benchmark')
