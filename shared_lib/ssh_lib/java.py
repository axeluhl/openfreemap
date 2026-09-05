from fabric import Connection

from .dnf import add_dnf_repo, dnf_install, dnf_remove


JAVA_VER = 25


def update_java(c: Connection) -> None:
    """Install OpenJDK (Temurin) from Eclipse Adoptium on Amazon Linux 2023."""
    # Remove any distro OpenJDK that might shadow Temurin.
    dnf_remove(c, 'java-*-openjdk*')

    # Configure the Eclipse Adoptium RPM repository (RHEL-family layout, which
    # Amazon Linux 2023 is compatible with).
    add_dnf_repo(
        c,
        'adoptium',
        '[Adoptium]\n'
        'name=Adoptium\n'
        'baseurl=https://packages.adoptium.net/artifactory/rpm/amazonlinux/2023/$basearch\n'
        'enabled=1\n'
        'gpgcheck=1\n'
        'gpgkey=https://packages.adoptium.net/artifactory/api/gpg/key/public\n',
    )

    dnf_install(c, f'temurin-{JAVA_VER}-jdk')

    c.run('java -version')
