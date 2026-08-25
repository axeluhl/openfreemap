import requests

from http_host_lib.config import config
from http_host_lib.shared import get_deployed_version
from http_host_lib.utils import assert_linux, assert_sudo


def fetch_version_files() -> bool:
    """
    Syncs the deployed_versions/<area>.txt pointer files.

    Default (auto-update) mode: the deployed version for each area is fetched
    from upstream (https://assets.openfreemap.com/deployed_versions/<area>.txt).

    local_versions mode (config.json "local_versions": true, set for instances
    seeded with --copy-runs-from-host): the upstream pointer is ignored and the
    deployed version is taken from the newest run actually present locally under
    runs/<area>/. This guarantees the pointer matches a locally mounted version,
    so nginx builds the "latest"/wildcard location block instead of falling
    through to `deny all` (HTTP 403), and it is never clobbered by an upstream
    value the instance doesn't hold.
    """

    print('Syncing local version files')

    assert_linux()
    assert_sudo()

    if config.ofm_config.get('local_versions'):
        return set_deployed_versions_from_local_runs()

    need_nginx_sync = False

    for area in config.areas:
        deployed_version = get_deployed_version(area)['version']
        if not deployed_version:
            print(f'  deployed version not found: {area}')
            continue
        print(f'  deployed version {area}: {deployed_version}')

        local_version_file = config.deployed_versions_dir / f'{area}.txt'

        try:
            local_version_old = local_version_file.read_text()
        except Exception:
            local_version_old = None

        if deployed_version != local_version_old:
            config.deployed_versions_dir.mkdir(exist_ok=True, parents=True)
            local_version_file.write_text(deployed_version)
            need_nginx_sync = True

    return need_nginx_sync


def set_deployed_versions_from_local_runs() -> bool:
    """
    local_versions mode: point deployed_versions/<area>.txt at the newest run
    present locally under runs/<area>/, without contacting upstream.

    Used for instances whose tiles were seeded from another host (via
    --copy-runs-from-host) rather than downloaded from openfreemap.org, so the
    deployed pointer always matches a version this instance actually holds.
    """

    print('  local_versions mode: using newest local run per area')

    need_nginx_sync = False

    for area in config.areas:
        area_dir = config.runs_dir / area
        if not area_dir.is_dir():
            print(f'  no local runs for {area}, skipping')
            continue

        local_versions = sorted(p.name for p in area_dir.iterdir() if p.is_dir())
        if not local_versions:
            print(f'  no local runs for {area}, skipping')
            continue

        newest_version = local_versions[-1]
        print(f'  deployed version {area}: {newest_version} (newest local run)')

        local_version_file = config.deployed_versions_dir / f'{area}.txt'

        try:
            local_version_old = local_version_file.read_text().strip()
        except Exception:
            local_version_old = None

        if newest_version != local_version_old:
            config.deployed_versions_dir.mkdir(exist_ok=True, parents=True)
            local_version_file.write_text(newest_version)
            need_nginx_sync = True

    return need_nginx_sync
