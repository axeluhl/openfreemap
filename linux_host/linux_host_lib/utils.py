import os
import shutil
import subprocess
import sys
from pathlib import Path

import requests


def assert_sudo():
    if os.geteuid() != 0:
        sys.exit('  needs sudo')


def assert_linux():
    if not sys.platform.startswith('linux'):
        sys.exit('  needs to be run on Linux')


def get_remote_file_size(url: str) -> int | None:
    r = requests.head(url, timeout=30)
    r.raise_for_status()
    size = r.headers.get('Content-Length')
    return int(size) if size else None


def download_file_aria2(url: str, local_file: Path) -> None:
    print(f'  downloading {url} into {local_file}')
    local_file.unlink(missing_ok=True)

    if shutil.which('aria2c'):
        args = [
            'aria2c',
            '--split=8',
            '--max-connection-per-server=8',
            '--file-allocation=none',
            '--min-split-size=1M',
            '-d',
            local_file.parent,
            '-o',
            local_file.name,
            url,
        ]
        subprocess.run(args, check=True)
        return

    # aria2 is not in the Amazon Linux 2023 base repos; fall back to wget, which is always
    # available. Slower (single connection) but functionally equal. The btrfs images are
    # huge (>100 GB), so make wget resilient to mid-stream drops: retry indefinitely, resume
    # the partial file in place, and use a read timeout so a stalled connection aborts and
    # retries instead of hanging. The caller still verifies size + SHA-256 afterwards.
    args = [
        'wget',
        '--continue',
        '--tries=0',
        '--timeout=60',
        '--waitretry=10',
        '--retry-connrefused',
        '-O',
        str(local_file),
        url,
    ]
    subprocess.run(args, check=True)
