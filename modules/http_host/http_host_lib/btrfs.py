import shutil
import subprocess
import sys

from http_host_lib.config import config
from http_host_lib.shared import get_versions_for_area
from http_host_lib.utils import download_file_aria2, get_remote_file_size


def download_area_version(area: str, version: str) -> bool:
    """
    Downloads and uncompresses tiles.btrfs files from the btrfs bucket

    "latest" version means the latest in the remote bucket
    "deployed" version means to read the currently deployed version string from the config dir
    """

    if area not in config.areas:
        sys.exit(f'  Please specify area: {config.areas}')

    versions = get_versions_for_area(area)
    if not versions:
        print(f'  No versions found for {area}')
        return False

    # latest version
    if version == 'latest':
        selected_version = versions[-1]

    # deployed version
    elif version == 'deployed':
        try:
            selected_version = (config.deployed_versions_dir / f'{area}.txt').read_text().strip()
        except Exception:
            return False

    # specific version
    else:
        if version not in versions:
            available_versions_str = '\n'.join(versions)
            print(
                f'  Requested version is not available.\nAvailable versions for {area}:\n{available_versions_str}'
            )
            return False
        selected_version = version

    return download_and_extract_btrfs(area, selected_version)


def download_and_extract_btrfs(area: str, version: str) -> bool:
    """
    returns True if download successful, False if skipped
    """

    print(f'Downloading btrfs: {area} {version}')

    version_dir = config.runs_dir / area / version
    btrfs_file = version_dir / 'tiles.btrfs'
    if btrfs_file.exists():
        print('  file exists, skipping download')
        return False

    temp_dir = config.runs_dir / '_tmp'
    target_file = temp_dir / 'tiles.btrfs.gz'

    # Sidecar marker recording which (area, version) the partial in _tmp belongs
    # to. _tmp is shared and reused across all sync calls (monaco/planet x
    # latest/deployed), and "latest" can roll to a newer version between runs, so
    # we resume the partial only when it matches exactly what we are fetching now.
    # Otherwise the partial is stale and _tmp is wiped for a fresh download.
    marker_file = temp_dir / 'partial.txt'
    marker = f'{area}/{version}'
    resume = (
        target_file.exists()
        and marker_file.exists()
        and marker_file.read_text().strip() == marker
    )
    if resume:
        print('  resuming partial download')
    else:
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True)
        marker_file.write_text(marker)

    url = f'https://btrfs.openfreemap.com/areas/{area}/{version}/tiles.btrfs.gz'

    # No disk-space pre-check: the only figure available here is the *compressed*
    # .gz size (its Content-Length; gzip's ISIZE trailer wraps at 4 GiB so the
    # uncompressed size can't be read from it either). A heuristic like
    # compressed * N can't bound the real footprint -- these images expand at a
    # ratio that swings widely (planet ~90 GB .gz -> ~170 GB btrfs), so any
    # margin safe enough to trust would forbid economic provisioning (~200 GB
    # partition). We still HEAD the URL so a missing/!bad remote fails cleanly;
    # if the disk is genuinely too small the download/unpigz below fails loudly.
    if not get_remote_file_size(url):
        print(f'  cannot get remote file size for {url}')
        return False

    download_file_aria2(url, target_file, resume=True)

    print('  uncompressing...')
    subprocess.run(['unpigz', temp_dir / 'tiles.btrfs.gz'], check=True)
    btrfs_src = temp_dir / 'tiles.btrfs'

    shutil.rmtree(version_dir, ignore_errors=True)
    version_dir.mkdir(parents=True)

    btrfs_src.rename(btrfs_file)

    shutil.rmtree(temp_dir)
    return True
