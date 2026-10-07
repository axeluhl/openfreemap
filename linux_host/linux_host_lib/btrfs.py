import shutil
import subprocess
import time
from datetime import UTC, datetime

import requests

from linux_host.linux_host_lib.linux_host_config import get_linux_host_config
from linux_host.linux_host_lib.telegram_alerts import send_telegram
from linux_host.linux_host_lib.utils import download_file_aria2, get_remote_file_size
from shared_lib.utils.cloudflare import CloudflareError


def prepare_version(area: str, version: str) -> None:
    """Download, verify, and atomically publish one version.

    Downloads the raw ``tiles.btrfs`` into the staging dir (``tmp/<area>/<version>``), verifies
    its SHA-256 against ``SHA256SUMS``, and then renames the staging dir to the final version
    dir. The staging and versions dirs live on the same volume (both under
    ``/data/ofm/linux_host``), so the rename is atomic and a half-written image is never
    published. ``download_file_aria2`` falls back to wget on AL2023 (aria2 is not in the base
    repos); see ``download_file_aria2`` in ``utils.py``.
    """
    version_dir = get_linux_host_config().versions_dir / area / version
    if (version_dir / 'tiles.btrfs').is_file():
        return

    print(f'Preparing {area} {version}', flush=True)

    shutil.rmtree(version_dir, ignore_errors=True)
    tmp_dir = get_linux_host_config().tmp_dir / area / version
    shutil.rmtree(tmp_dir, ignore_errors=True)

    base_url = f'https://btrfs.openfreemap.com/areas/{area}/{version}'
    url = f'{base_url}/tiles.btrfs'
    tmp_file = tmp_dir / 'tiles.btrfs'

    try:
        response = requests.get(f'{base_url}/SHA256SUMS', timeout=30)
        response.raise_for_status()
        expected_hash = next(
            (
                parts[0]
                for line in response.text.splitlines()
                if len(parts := line.split()) >= 2 and parts[1] == 'tiles.btrfs'
            ),
            None,
        )
        if not expected_hash:
            raise CloudflareError('tiles.btrfs is missing from SHA256SUMS')

        remote_size = get_remote_file_size(url)

        tmp_dir.mkdir(parents=True)
        needed_space = remote_size + 1024**3
        free_space = shutil.disk_usage(tmp_dir).free
        if free_space < needed_space:
            raise RuntimeError(
                f'not enough disk space. Needed: {needed_space}, free space: {free_space}'
            )

        start = time.monotonic()
        print(f'  downloading {url} ({remote_size} bytes)', flush=True)
        download_file_aria2(url, tmp_file)
        if tmp_file.stat().st_size != remote_size:
            raise CloudflareError(
                f'incorrect file size: expected {remote_size}, got {tmp_file.stat().st_size}'
            )

        print(f'  verifying SHA-256 of {tmp_file.name}', flush=True)
        digest = subprocess.run(
            ['sha256sum', str(tmp_file)], capture_output=True, text=True, check=True
        ).stdout.split()[0]
        if digest.lower() != expected_hash.lower():
            raise RuntimeError(f'SHA-256 mismatch for {url}')

        version_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.rename(version_dir)
        print(f'  finished {area} {version}', flush=True)
    except Exception:
        # Preparation only removes its disposable attempt and raises. The sync
        # caller decides whether this version is required or an optional prefetch.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(version_dir, ignore_errors=True)
        raise

    ledger = get_linux_host_config().download_ledger
    lines = ledger.read_text().splitlines() if ledger.is_file() else []
    count = sum(1 for line in lines if line.split()[1:3] == [area, version])
    with ledger.open('a') as f:
        f.write(
            f'{datetime.now(UTC).isoformat(timespec="seconds")} {area} {version} {remote_size}\n'
        )
    gb = remote_size / 1e9
    minutes = (time.monotonic() - start) / 60
    send_telegram(
        f'downloaded {area} {version} ({gb:.1f} GB, {minutes:.0f} min, download #{count + 1})',
        silent=count == 0,
    )
