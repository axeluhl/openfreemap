from linux_host.linux_host_lib.linux_host_config import get_linux_host_config
from linux_host.linux_host_lib.telegram_alerts import send_telegram_alert
from shared_lib.utils.get_version import get_deployed_version


def get_remote_deployed_versions() -> dict[str, str]:
    print('Fetching remote deployed version files')

    remote_versions: dict[str, str] = {}
    for area in get_linux_host_config().areas:
        try:
            deployed_version = get_deployed_version(area)['version']
        except Exception as e:
            print(f'cannot fetch deployed version for {area}: {type(e).__name__}: {e}')
            continue

        if not deployed_version:
            print(f'  deployed version not found: {area}')
            continue

        print(f'  remote deployed version {area}: {deployed_version}')
        remote_versions[area] = deployed_version

    return remote_versions


def get_local_deployed_versions() -> dict[str, str]:
    local_versions: dict[str, str] = {}
    for area in get_linux_host_config().areas:
        version_file = get_linux_host_config().deployed_versions_dir / f'{area}.txt'
        try:
            version = version_file.read_text().strip()
        except OSError:
            continue

        if (get_linux_host_config().versions_dir / area / version / 'tiles.btrfs').is_file():
            local_versions[area] = version

    return local_versions


def get_local_run_versions() -> dict[str, str]:
    """local_versions mode: newest run present locally under versions/<area>/ per area.

    Used for instances seeded from another host (golden AMI / copied runs) rather than
    downloaded from upstream, so the deployed pointer always matches a version this instance
    actually holds. Only runs with a materialized tiles.btrfs are considered.
    """
    local_versions: dict[str, str] = {}
    for area in get_linux_host_config().areas:
        area_dir = get_linux_host_config().versions_dir / area
        if not area_dir.is_dir():
            print(f'  no local runs for {area}, skipping')
            continue

        versions = sorted(p.name for p in area_dir.iterdir() if (p / 'tiles.btrfs').is_file())
        if not versions:
            print(f'  no local runs for {area}, skipping')
            continue

        newest = versions[-1]
        print(f'  deployed version {area}: {newest} (newest local run)')
        local_versions[area] = newest

    return local_versions


def write_version_files(remote_versions: dict[str, str]) -> None:
    for area, deployed_version in remote_versions.items():
        if not (
            get_linux_host_config().versions_dir / area / deployed_version / 'tiles.btrfs'
        ).is_file():
            message = f'not switching {area} to {deployed_version}: local btrfs is missing'
            send_telegram_alert(f'ERROR\n{message}')
            continue

        local_version_file = get_linux_host_config().deployed_versions_dir / f'{area}.txt'
        try:
            local_version_old = local_version_file.read_text().strip()
        except OSError:
            local_version_old = None

        if deployed_version != local_version_old:
            get_linux_host_config().deployed_versions_dir.mkdir(exist_ok=True, parents=True)
            local_version_file.write_text(deployed_version)
