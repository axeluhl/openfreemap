from fabric import Connection

from .utils import put_str


def kernel_somaxconn65k(c: Connection) -> None:
    put_str(c, '/etc/sysctl.d/60-somaxconn65k.conf', 'net.core.somaxconn = 65535')


def kernel_limits1m(c: Connection) -> None:
    put_str(
        c,
        '/etc/security/limits.d/limits1m.conf',
        """
            * soft nofile 1048576
            * hard nofile 1048576
            root soft nofile 1048576
            root hard nofile 1048576
        """,
    )
