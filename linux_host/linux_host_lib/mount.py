import subprocess
from pathlib import Path

from linux_host.linux_host_lib.linux_host_config import get_linux_host_config


def reconcile_mounts(retained_versions: dict[str, set[str]]) -> None:
    """Persist every locally present image to /etc/fstab and `mount -a`.

    Mirrors upstream's auto_mount/create_fstab: the loop-mounts are written to /etc/fstab (which
    lives on the root volume) so they survive a reboot -- and, crucially, an AMI bake: a golden
    AMI captures both the tiles.btrfs files and their fstab entries, and local-fs.target re-mounts
    them on boot before nginx starts. An imperative mount without fstab would serve until the next
    reboot and then come up with nothing mounted on a launched AMI (the regression this restores).

    fstab is regenerated from the images actually present under versions/<area>/ on every sync (all
    prior /mnt/ofm/ lines are replaced), so it needs no hand-maintained version pinning. Retention
    is enforced separately by garbage_collect(); `retained_versions` is accepted for call-site
    compatibility and to assert the versions meant to be served are actually mounted.
    """
    create_fstab()

    print('  running mount -a')
    subprocess.run(['mount', '-a'], check=True)

    # The images this host must serve now must be mounted and carry their metadata; fail loudly
    # otherwise rather than let nginx serve an area whose tiles are not there.
    for area, versions in retained_versions.items():
        for version in sorted(versions):
            if not (
                get_linux_host_config().versions_dir / area / version / 'tiles.btrfs'
            ).is_file():
                continue
            mnt = get_linux_host_config().mnt_dir / f'{area}-{version}'
            if not (mnt / 'metadata.json').is_file():
                raise RuntimeError(f'mounted version is missing metadata.json: {area} {version}')


def create_fstab() -> None:
    """Rewrite the /mnt/ofm/ loop-mount lines in /etc/fstab to every image present on disk.

    Enumerates versions/<area>/<version>/tiles.btrfs, drops all existing lines pointing under the
    mnt dir, and appends one `... btrfs loop,ro 0 0` entry per image. Every other fstab line is
    preserved. Adapted from upstream create_fstab() to the linux_host versions/ layout.
    """
    print('  creating fstab')
    mnt_dir = get_linux_host_config().mnt_dir
    versions_dir = get_linux_host_config().versions_dir

    fstab_new = []
    for area in get_linux_host_config().areas:
        area_dir = versions_dir / area
        if not area_dir.is_dir():
            continue
        for version in sorted(area_dir.iterdir()):
            btrfs_file = version / 'tiles.btrfs'
            if not btrfs_file.is_file():
                print(f"  {btrfs_file} doesn't exist, skipping")
                continue
            mnt_folder = mnt_dir / f'{area}-{version.name}'
            mnt_folder.mkdir(exist_ok=True, parents=True)
            fstab_new.append(f'{btrfs_file} {mnt_folder} btrfs loop,ro 0 0\n')
            print(f'  created fstab entry for {mnt_folder}')

    fstab_path = Path('/etc/fstab')
    fstab_orig = [
        line
        for line in fstab_path.read_text().splitlines(keepends=True)
        if f'{mnt_dir}/' not in line
    ]
    if fstab_orig and not fstab_orig[-1].endswith('\n'):
        fstab_orig[-1] += '\n'
    fstab_path.write_text(''.join(fstab_orig + fstab_new))
