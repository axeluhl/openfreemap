# Self-hosting Howto

You can either self-host or use our public instance. Everything is **open-source**, including the full production setup — there’s no 'open-core' model here.

When self-hosting, there are two modules you can set up on a server (see details in the repo README).

- **http-host**

- **tile-gen**

There is a 99.9% chance you only need **http-host**. Tile-gen is slow, needs a huge machine and is totally pointless, since we upload the processed files every week.

### System requirements

**http-host**: 300 GB disk space for hosting a single run. SSD is recommended, but not required. NVMe even better.

**tile-gen**: 500 GB SDD and at least 64 GB ram

**Amazon Linux 2023 (AL2023)** or newer

### Provider recommendation

One amazing deal, which is tested and known to work well for http-host is the €4.5 / month [Contabo Storage VPS](https://contabo.com/en/storage-vps/)

> **Background read:** if you self-host on AWS, see [AWS EC2 instance performance for http-host](benchmark/ec2_instance_performance.md) — load-testing shows tile serving is network-egress-bound (not CPU-bound), so a cheap `t4g.small` outperforms a much pricier `c6gd.xlarge`.

---

### Warning

This project is made to run on **clean servers** or virtual machines dedicated for this project. The scripts need sudo permissions as they mount/unmount disk images. Do not run this on your dev machine without using virtual machines. If you do, please make sure you understand exactly what each script is doing.

If you run it on a non-clean server, please understand that this will modify your nginx config!

---

## Instructions

I recommend running things quickly first, with `SKIP_PLANET=true` and then once it works, running it with `SKIP_PLANET=false`.

#### 1. DNS/ALB setup

Get your **Route53 --> ALB --> Target Group --> Instance(s)** set-up right. SSL offloading is expected to happen
at the ALB, ideally using AWS-provided certificates. The instances will handle only HTTP traffic on port 80.
A good health check seems `/styles/liberty` to see if a JSON comes back with a HTTP 200 status code.

#### 2. Clone and prepare `config` folder

```
git clone https://github.com/axeluhl/openfreemap
```

In the config folder, copy `.env.sample` to `.env` and set the values.

`DOMAIN_DIRECT` - Your subdomain

This is the public hostname that nginx serves under (its `server_name`). You can also override it per run
with the `--domain` switch on the command line (see step 5), which takes precedence over `DOMAIN_DIRECT`.

Set `SKIP_PLANET=true` first.

#### 3. Set up Python if you don't have it yet

On Ubuntu you can get it by `sudo apt install python3-pip`

On macOS you can do `brew install python`

#### 4. Prepare the Python environment

You run the deploy script locally, and it deploys to a remote server over SSH.
The dependencies are `fabric`, `click` and `python-dotenv`.

```
cd openfreemap
pip install -e .
```

On recent, externally managed Python installations (PEP 668 — e.g. Ubuntu
24.04 LTS, Debian 12), a plain `pip install` fails with
`error: externally-managed-environment`. Use **one** of these alternatives:

**a) Install the dependencies via apt (simplest on Ubuntu/Debian):**

```
sudo apt install python3-fabric python3-click python3-dotenv
```

Then run `./init-server.py ...` directly with the system Python — no `pip
install -e .` needed.

**b) Use an isolated virtualenv:**

```
python3 -m venv .venv          # sudo apt install python3-venv if missing
source .venv/bin/activate
pip install -e .
```

Remember to `source .venv/bin/activate` in each new shell before running
`./init-server.py`.

**c) Override the guard (not recommended):** `pip install -e . --break-system-packages`.

#### 5. Deploy quick version with `SKIP_PLANET=true`

Run the actual deploy command and wait a few minutes

```
./init-server.py http-host-static HOSTNAME --domain maps.example.com
```

`HOSTNAME` is the SSH target (the machine to deploy to) and is the only positional argument. The SSH **user** is
*not* a separate positional argument — provide it either via your `~/.ssh/config`, via the `--user` option, or by
embedding it in the hostname as `user@host` (Fabric shorthand). Likewise a non-standard SSH port comes from
`~/.ssh/config` or the `--port` option. You do not need to log in as `root`: the deploy tasks call `sudo`
themselves, so a normal login user with (passwordless) sudo rights is enough. On an AWS EC2 Amazon Linux instance
that user is `ec2-user`:

```
./init-server.py http-host-static ec2-user@HOSTNAME --domain maps.example.com
# or equivalently
./init-server.py http-host-static HOSTNAME --user ec2-user --domain maps.example.com
```

`--domain` is the public hostname that nginx will answer under (its `server_name`); it overrides the
`DOMAIN_DIRECT` value from your `.env`. If you omit `--domain`, the `DOMAIN_DIRECT` value from `.env` is used
instead:

```
./init-server.py http-host-static HOSTNAME
```

The `--domain` switch is available on the `http-host-static`, `http-host-autoupdate` and `http-host-sync`
commands. Use `http-host-sync --domain ...` to update the `server_name` of an already-deployed host without
redeploying.

##### Faster provisioning options (AWS / large fleets)

The `http-host-static` and `http-host-autoupdate` commands support a few extra switches that speed up bringing
up a new host when you already run other hosts or have local NVMe available.

**Local NVMe for the btrfs download** — On by default. Before the download runs, the deploy looks for an
*unformatted* NVMe disk (no filesystem, no partitions, not mounted) large enough to hold the ~90 GB **gzipped**
btrfs download, partitions it (GPT, one partition), formats that partition with ext4 and mounts it at the download
staging dir (`/data/ofm/http_host/runs/_tmp`). The `.gz` download then lands on fast, cheap local NVMe and is
decompressed **straight onto the runs volume**, so the extracted multi-hundred-GB `tiles.btrfs` stays on the
"real" root/EBS volume — where an AMI created off the instance will capture it — while only the throwaway download
bytes live on the NVMe. Since instance-store NVMe is never part of an AMI, the download data is automatically
excluded from any image. Existing/formatted disks (including the root disk) are never touched, and if no suitable
disk is found the download goes onto the runs volume as before.

- `--no-nvme` — disable the NVMe search entirely and download onto the runs volume.
- `--nvme-min-size-gb N` — minimum disk size to qualify (default `100`, sized for the ~90 GB gzipped planet download).

Note: instance-store NVMe is *ephemeral* — it is wiped on stop/start. The fstab entry uses `nofail`, so a later
stop/start (which brings the volume back blank) never blocks boot; the download then simply falls back to the runs
volume. The extracted `tiles.btrfs` already lives on EBS, so it survives the stop/start regardless.

**Copy the btrfs runs from an existing host** — Instead of downloading the runs from the web, copy them (for all
areas) directly from a host you already run, via `scp`. Planet is the large one; the other areas are tiny and
copied along with it. The subsequent sync finds each `tiles.btrfs` already present and skips its download.

- `--copy-runs-from-host HOST` — copy `/data/ofm/http_host/runs` from this host over `scp`.
- `--copy-runs-user USER` — SSH user for that host (defaults to the target `--user` / your ssh login user).

The `scp` runs *on the new host* as your login user and authenticates to the source host using your **forwarded
ssh-agent** (agent forwarding is enabled automatically). So make sure your local `ssh-agent` holds a key that can
reach the source host (`ssh-add`) before running the deploy. The source host key is auto-accepted
(`StrictHostKeyChecking=accept-new`).

```
./init-server.py http-host-static ec2-user@NEWHOST --domain maps.example.com \
    --copy-runs-from-host 10.0.0.5 --nvme-min-size-gb 100
```

**Local vs. upstream version management (`--local-versions`)** — Each instance records, in its own
`config.json`, whether it manages the *deployed version* locally or tracks the upstream openfreemap.org pointer:

- **Upstream mode (default):** on every sync the deployed version is fetched from
  `https://assets.openfreemap.com/deployed_versions/<area>.txt` and the matching btrfs is downloaded. Use this for
  instances that should follow the official latest release.
- **Local mode (`local_versions: true`):** the upstream pointer is ignored; the deployed version is taken from the
  newest run present locally under `runs/<area>/`. This is the correct mode for instances seeded with
  `--copy-runs-from-host`, whose tiles come from another host rather than a download.

Why this matters: nginx only builds the `latest`/wildcard tile location block for a version that is *both* pointed
to by `deployed_versions/<area>.txt` *and* actually mounted. If a copied instance's pointer were left tracking an
upstream version it doesn't hold, that block would be skipped and every non-exact-version request would fall
through to `deny all` — i.e. **HTTP 403** (a genuinely missing tile inside a valid version returns a `200` empty
tile instead). Local mode keeps the pointer aligned with the mounted tiles, so this never happens.

The mode is **per instance and sticky**: `--copy-runs-from-host` turns local mode on automatically, and re-running
`http-host-static` / `http-host-sync` later preserves whatever the instance already had. Override explicitly with
`--local-versions` / `--no-local-versions` (e.g. to later convert a copied instance to follow upstream).

#### 5. Check

If everything is OK, you'll have some curl lines printed. Run the first one locally and make sure it's showing HTTP/2 200. For example this is an OK response.

```locally to test them.
curl -sI https://test.openfreemap.org/monaco | sort

HTTP/2 200
access-control-allow-origin: *
cache-control: max-age=86400
cache-control: public
content-length: 5776
content-type: application/json
date: Fri, 11 Oct 2024 21:01:23 GMT
etag: "670991d1-1690"
expires: Sat, 12 Oct 2024 21:01:23 GMT
last-modified: Fri, 11 Oct 2024 21:00:01 GMT
server: nginx
x-ofm-debug: latest JSON monaco
```

#### 6. Deploy and check with `SKIP_PLANET=false`

Update your `.env` file and re-run the same `./init-server.py http-host-static HOSTNAME --domain maps.example.com` as before.

Go for a walk and by the time you come back it should be up and running with the latest planet tiles deployed. Don't worry about the "Download aborted" lines in the meanwhile, it's a bug in CloudFlare.

If your server doesn't have an SSD, the download + uncompressing process can take hours.

---

#### Deploy tile-gen server (optional)

If you have a really beefy machine (see above) and you really want to generate tiles yourself, you can run `./init-server.py tile-gen HOSTNAME`.

Trigger a run manually, by running

```
sudo /data/ofm/venv/bin/python -u /data/ofm/tile_gen/bin/tile_gen.py make-tiles planet
```

It's recommended to use tmux or similar, as it can take days to complete.
