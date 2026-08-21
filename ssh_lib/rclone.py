from ssh_lib.utils import exists


def rclone(c):
    if exists(c, '/usr/bin/rclone'):
        return

    c.sudo('curl https://rclone.org/install.sh | sudo bash')
