from ssh_lib.utils import pkg_install, pkg_remove, put_str


JAVA_VER = 24


def java(c):
    """Install OpenJDK (Temurin) from Eclipse Adoptium on Amazon Linux 2023."""
    # remove any distro OpenJDK that might shadow Temurin
    pkg_remove(c, 'java-*-openjdk*')

    # Configure the Eclipse Adoptium RPM repository (RHEL-family layout, which
    # Amazon Linux 2023 is compatible with).
    put_str(
        c,
        '/etc/yum.repos.d/adoptium.repo',
        '[Adoptium]\n'
        'name=Adoptium\n'
        'baseurl=https://packages.adoptium.net/artifactory/rpm/amazonlinux/2023/$basearch\n'
        'enabled=1\n'
        'gpgcheck=1\n'
        'gpgkey=https://packages.adoptium.net/artifactory/api/gpg/key/public\n',
    )

    pkg_install(c, f'temurin-{JAVA_VER}-jdk')

    # Verify installation
    c.run('java -version')
