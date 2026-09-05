import shutil
import subprocess

import requests

from linux_host.linux_host_lib.linux_host_config import get_linux_host_config
from linux_host.linux_host_lib.utils import download_file_aria2, get_remote_file_size


def prepare_version(area: str, version: str) -> None:
    """Download, verify, and atomically publish one version.

    The compressed ``tiles.btrfs.gz`` is downloaded into the staging dir (``tmp/<area>/<version>``)
    and then stream-decompressed with ``unpigz -c`` straight onto the versions volume. This keeps
    the disposable ~90 GB ``.gz`` and the final ~165 GB ``tiles.btrfs`` on separate volumes when
    the staging dir is a mounted ephemeral NVMe (see ``mount_nvme_download_volume`` in the deploy):
    the throwaway ``.gz`` lives on the NVMe (never captured by an AMI) while the extracted image
    lands on the root/EBS volume. When no NVMe is mounted, both simply share the linux_host volume.

    Decompressing (a stream copy) instead of a plain ``rename`` is what makes the split possible:
    a rename across filesystems fails with EXDEV. The final rename of ``tiles.btrfs.tmp`` ->
    ``tiles.btrfs`` happens within the versions volume, so a half-written image is never published.
    """
    version_dir = get_linux_host_config().versions_dir / area / version
    if (version_dir / 'tiles.btrfs').is_file():
        return

    shutil.rmtree(version_dir, ignore_errors=True)
    tmp_dir = get_linux_host_config().tmp_dir / area / version
    shutil.rmtree(tmp_dir, ignore_errors=True)

    base_url = f'https://btrfs.openfreemap.com/areas/{area}/{version}'
    gz_url = f'{base_url}/tiles.btrfs.gz'
    btrfs_url = f'{base_url}/tiles.btrfs'
    gz_file = tmp_dir / 'tiles.btrfs.gz'
    btrfs_tmp = version_dir / 'tiles.btrfs.tmp'
    btrfs_file = version_dir / 'tiles.btrfs'

    try:
        expected_gz_hash = get_sha256(base_url, 'tiles.btrfs.gz')

        gz_size = get_remote_file_size(gz_url)
        if gz_size is None:
            raise RuntimeError(f'cannot get remote file size for {gz_url}')

        # The staging dir (possibly ephemeral NVMe) must hold the compressed .gz.
        tmp_dir.mkdir(parents=True)
        gz_needed = gz_size + 1024**3
        gz_free = shutil.disk_usage(tmp_dir).free
        if gz_free < gz_needed:
            raise RuntimeError(
                f'not enough download (staging) disk space. '
                f'Needed: {gz_needed}, free space: {gz_free}'
            )

        # The versions volume (root/EBS) must hold the extracted, uncompressed image.
        btrfs_size = get_remote_file_size(btrfs_url)
        if btrfs_size is None:
            raise RuntimeError(f'cannot get remote file size for {btrfs_url}')
        version_dir.mkdir(parents=True, exist_ok=True)
        btrfs_needed = btrfs_size + 1024**3
        btrfs_free = shutil.disk_usage(version_dir).free
        if btrfs_free < btrfs_needed:
            raise RuntimeError(
                f'not enough versions disk space. Needed: {btrfs_needed}, free space: {btrfs_free}'
            )

        download_file_aria2(gz_url, gz_file)
        if gz_file.stat().st_size != gz_size:
            raise RuntimeError(
                f'incorrect file size: expected {gz_size}, got {gz_file.stat().st_size}'
            )

        digest = subprocess.run(
            ['sha256sum', str(gz_file)], capture_output=True, text=True, check=True
        ).stdout.split()[0]
        if digest.lower() != expected_gz_hash.lower():
            raise RuntimeError(f'SHA-256 mismatch for {gz_url}')

        # Stream-decompress from the (possibly NVMe) staging dir onto the versions volume.
        # unpigz verifies the gzip CRC, so a correctly-decompressed image is guaranteed.
        with open(btrfs_tmp, 'wb') as out:
            subprocess.run(['unpigz', '-c', str(gz_file)], stdout=out, check=True)
        if btrfs_tmp.stat().st_size != btrfs_size:
            raise RuntimeError(
                f'incorrect extracted size: expected {btrfs_size}, got {btrfs_tmp.stat().st_size}'
            )

        btrfs_tmp.rename(btrfs_file)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        # Preparation only removes its disposable attempt and raises. The sync
        # caller decides whether this version is required or an optional prefetch.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(version_dir, ignore_errors=True)
        raise


def get_sha256(base_url: str, filename: str) -> str:
    response = requests.get(f'{base_url}/SHA256SUMS', timeout=30)
    response.raise_for_status()
    expected_hash = next(
        (
            parts[0]
            for line in response.text.splitlines()
            if len(parts := line.split()) >= 2 and parts[1] == filename
        ),
        None,
    )
    if not expected_hash:
        raise RuntimeError(f'{filename} is missing from SHA256SUMS')
    return expected_hash
