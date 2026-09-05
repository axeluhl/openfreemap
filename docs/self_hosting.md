# Self-hosting Howto

You can either self-host or use our public instance. Everything is **open-source**, including the full production setup — there’s no 'open-core' model here.

When self-hosting, there are two modules you can set up on a server (see details in the repo README).

- **linux_host**

- **tilegen**

There is a 99.9% chance you only need **linux_host**. tilegen is slow, needs a huge machine and is totally pointless, since we upload the processed files every week.

### System requirements

**linux_host**: 300 GB disk space for hosting a single planet run. SSD is recommended, but not required. Note that an `auto_update: true` host may hold TWO complete versions during a release transition (the active version plus a prefetched candidate), so provide capacity for two complete versions on automatic planet hosts.

**tilegen**: 500 GB SDD and at least 64 GB ram

**Amazon Linux 2023 (AL2023)** or newer

> This fork targets **Amazon Linux 2023** (dnf-based provisioning). Upstream OpenFreeMap targets Ubuntu 24.04 — if
> you deploy to Ubuntu, use the upstream repo instead.

### Provider recommendation

One amazing deal, which is tested and known to work well for linux_host is the €4.5 / month [Contabo Storage VPS](https://contabo.com/en/storage-vps/)

> **Background read:** if you self-host on AWS, see [AWS EC2 instance performance for linux_host](benchmark/ec2_instance_performance.md) — load-testing shows tile serving is network-egress-bound (not CPU-bound), so a cheap `t4g.small` outperforms a much pricier `c6gd.xlarge`.

---

### Warning

This project is made to run on **clean servers** or virtual machines dedicated for this project. The scripts need sudo permissions as they mount/unmount disk images. Do not run this on your dev machine without using virtual machines. If you do, please make sure you understand exactly what each script is doing.

If you run it on a non-clean server, please understand that this will modify your nginx config!

---

## Instructions

I recommend running things quickly first, with `"areas": ["monaco"]` and then once it works, running it with `"areas": ["planet", "monaco"]`.

1.  **DNS / TLS setup**

    This fork is designed to run **behind an AWS Application Load Balancer (ALB)** with TLS offloaded at the ALB.
    Get your **Route53 → ALB → Target Group → Instance(s)** set-up right. SSL offloading happens at the ALB, ideally
    using AWS-provided certificates. With `cert: alb` (see below) the instances handle only HTTP traffic on port 80.
    A good ALB health check target is `/healthz/planet` (or `/healthz/monaco`) with success code **204** (see the
    golden-AMI section). `/styles/liberty` returning JSON with an HTTP 200 status code also works as a check.

    If you are not using an ALB, choose a TLS-terminating `cert` type (`letsencrypt`, `upload` or `dummy`) instead and
    the instance handles HTTPS itself on port 443.

1.  **Clone and prepare the `config` folder**

    ```
    git clone https://github.com/axeluhl/openfreemap
    cd openfreemap
    cp config/linux_host/config.sample.jsonc config/linux_host/self-hosted.jsonc
    ```

    Edit `config/linux_host/self-hosted.jsonc` and fill it out:

    - replace `tiles.example.com` with your own domain (this is nginx's `server_name` and drives the generated
      TileJSON/style URLs)
    - choose a **certificate type**:
        - `alb` — plain HTTP on port 80 only; TLS is terminated at an AWS ALB in front of the instance (this fork's
          default deployment model). Installs no certificate and does no HTTP→HTTPS redirect.
        - `letsencrypt` — the instance obtains a Let's Encrypt certificate over ACME (set your `email`)
        - `upload` — provide your own certificate/key files (`cert_path` / `key_path`)
        - `dummy` — self-signed, for local testing only

      See the comments in the sample for the exact syntax.
    - optionally set `hosts` — but you normally pass the deploy IP on the command line with `--host [user@]host`
      instead (see below), so `hosts` may be empty or omitted
    - optionally set `ssh_user` — the default SSH user when `--host` has no `user@` prefix and `--user` is not given
      (defaults to `ec2-user`)
    - set `auto_update`: `true` installs a once-per-minute sync cron (deployment is asynchronous); `false` starts one
      detached sync session at deploy time and installs no cron
    - set `areas`: use `["monaco"]` for the first quick deploy, then `["planet", "monaco"]` for the full deploy
    - optionally set `local_versions: true` — see *Local vs. upstream version management* below (used for golden-AMI
      fleets)

1.  **Install `uv` locally and sync the environment**

    You run the deploy script locally, and it deploys to a remote server over SSH.
    Install [uv](https://docs.astral.sh/uv/) and make sure it is on your `PATH`, then sync the local environment:

    ```
    uv sync
    ```

1.  **Deploy quick version with `"areas": ["monaco"]`**

    Run the actual deploy command. The config name maps to `config/linux_host/self-hosted.jsonc`:

    ```
    ./linux_host/deploy_linux_host.py --config self-hosted --host <IP-ADDRESS>
    ```

    `--host` takes `[user@]host` and is authoritative: the given host is used as-is and does **not** need to appear
    in the config's `hosts` array (the config describes the *type* of setup, not the volatile deploy IP). If you omit
    `--host`, the deploy falls back to the config's `hosts` (and requires exactly one entry, or `--host` to pick one).

    The deploy script connects over SSH. You can SSH as `root` or as a normal sudo-capable user; the script creates
    and uses an `ofm` runtime user. On an AWS EC2 Amazon Linux instance the login user is `ec2-user`, which is the
    default SSH user. Override it with `ec2-user@<IP>` in `--host`, with `--user YOUR_SSH_USER`, or with `ssh_user` in
    the config; add `--port 22` if needed.

    For password-based SSH, set `SSH_PASSWD`. If sudo uses a different password, set `SUDO_PASSWD` too:

    ```
    SSH_PASSWD='your-ssh-password' SUDO_PASSWD='your-sudo-password' ./linux_host/deploy_linux_host.py --config self-hosted --host HOSTNAME --user YOUR_SSH_USER --port 22
    ```

    Deployment takes each target host offline while it rebuilds the serving setup. It disables scheduling, stops sync
    and nginx processes, unmounts images, and recreates disposable runtime state. It preserves downloaded assets,
    nginx ACME state, and complete images for configured areas. It removes images for areas that are no longer
    configured.

1.  **Check**

    Deployment is asynchronous: the deploy command does not print curl lines and does not wait for tiles to become
    live. It prints a success message and a MapLibre style URL, `https://YOUR_DOMAIN/styles/liberty`.

    - With `auto_update: true`, the once-per-minute cron downloads and serves the tiles in the background.
    - With `auto_update: false`, the deploy prints a tmux attach command so you can watch the one-off sync.

    Once the sync has finished, verify it yourself. Run this locally and make sure it shows HTTP/2 200. For example
    this is an OK response:

    ```
    curl -sI https://YOUR_DOMAIN/monaco

    HTTP/2 200
    access-control-allow-origin: *
    cache-control: max-age=86400
    cache-control: public
    content-length: 5776
    content-type: application/json
    server: nginx
    x-ofm-debug: latest JSON monaco
    ```

    `https://YOUR_DOMAIN/planet/latest` always points to the active deployed Planet TileJSON, and
    `/planet/latest/{z}/{x}/{y}.pbf` serves its tiles. Any non-existing version also serves the active version.

1.  **Deploy and check with `"areas": ["planet", "monaco"]`**

    Edit `config/linux_host/self-hosted.jsonc` to set `"areas": ["planet", "monaco"]` and re-run the same
    `./linux_host/deploy_linux_host.py --config self-hosted [--host HOSTNAME]` as before.

    Go for a walk and by the time you come back it should be up and running with the latest planet tiles deployed.
    Don't worry about the "Download aborted" lines in the meanwhile, it's a bug in CloudFlare. If your server doesn't
    have an SSD, the download + decompression process can take hours.

### Synchronization and retained versions

A sync keeps the active deployed version available while it downloads and verifies a replacement in full. Verified
images live under `versions/`. Each download starts from zero in a disposable `tmp/` directory; downloads are not
resumed.

### Local vs. upstream version management (`local_versions`)

Each instance records, in its own `config.jsonc`, whether it manages the *deployed version* locally or tracks the
upstream openfreemap.org pointer:

- **Upstream mode (default):** on every sync the deployed version is fetched from
  `https://assets.openfreemap.com/deployed_versions/<area>.txt` and the matching btrfs is downloaded. Use this for
  instances that should follow the official latest release.
- **Local mode (`local_versions: true`):** the upstream pointer is ignored and no btrfs is downloaded; the deployed
  version is taken from the newest run present locally under `versions/<area>/`. This is the correct mode for
  golden-AMI fleet instances that boot already holding the tiles baked into the image.

Why this matters: nginx only builds the `latest`/wildcard tile location block for a version that is *both* pointed to
by the deployed-version pointer *and* actually mounted. If a baked instance's pointer were left tracking an upstream
version it does not hold, that block would be skipped and every non-exact-version request would fall through to
`deny all` — i.e. **HTTP 403** (a genuinely missing tile inside a valid version returns a `200` empty tile instead).
Local mode keeps the pointer aligned with the mounted tiles, so this never happens.

### Baking a golden AMI for an Auto Scaling Group behind an ALB

This is the recommended way to run a fleet: build the tiles **once** on a big Graviton box, snapshot its root volume
into an AMI, then boot cheap `t4g.small` instances from that AMI. Each instance comes up already holding the planet
tiles on its own root disk — no per-instance download — so it can serve within seconds of boot and slot straight into
an ALB target group managed by an Auto Scaling Group.

The whole architecture stays **arm64**: the bake instance is Graviton, so the AMI is arm64, so it runs on
`t4g.small`. Do not bake on an x86 instance.

**1. Launch the bake instance.** From a clean **Amazon Linux 2023 (arm64)** image:

- Instance type **`m6gd.large`** (2 vCPU, 8 GB RAM, 1×118 GB instance-store NVMe). The 118 GB NVMe is a good fit for
  the ~90 GB **gzipped** planet download (see *Local NVMe for the download* below); any Graviton type with a similarly
  sized instance-store NVMe works. If you use `--copy-runs-from-host` (below) you don't need the NVMe and a plain
  `t4g.small`/`m6g.large` is fine.
- A single **200 GB gp3 root** EBS volume — same size the `t4g.small` fleet will run, so the AMI is sized right. It
  must hold the extracted, **uncompressed** `tiles.btrfs` (~165 GB for planet). During the bake, temporarily raise the
  root volume's gp3 throughput (e.g. to 500–1000 MB/s) so the large write to EBS is fast; runtime reads on
  `t4g.small` are unaffected by that setting.
- The 118 GB NVMe is left **unformatted** — the deploy detects it and uses it to stage the download (see *Local NVMe
  for the download* below). It is instance-store, so it is **never captured in the AMI**; only the extracted
  `tiles.btrfs` on the root/EBS volume is.
- Security group: inbound SSH (22) from your workstation, and outbound HTTPS (443) so it can download the tiles.

**2. Bake with `cert: alb` and `local_versions: true`.** Prepare a bake config (e.g.
`config/linux_host/bake.jsonc`) with:

- `"cert": { "type": "alb" }` — the fleet serves plain HTTP:80 behind the ALB,
- `"auto_update": false` — a golden AMI should serve a **fixed, immutable** version; you do not want each auto-scaled
  instance re-downloading tiles or drifting to a different version at boot,
- `"local_versions": true` — so booted fleet instances serve the baked tiles without contacting the upstream pointer,
- `"areas": ["planet", "monaco"]`.

You pass the bake instance's IP on the command line with `--host` (below), so `hosts` in the config can be left
empty or omitted.

Run the deploy from your workstation and wait for the one-off sync to download and decompress the tiles onto the root
volume (this is the long step; the "Download aborted" lines from CloudFlare in the meantime are harmless):

```
./linux_host/deploy_linux_host.py --config bake --host <IP-ADDRESS> --user ec2-user
```

**Local NVMe for the download (on by default).** Before the download runs, the deploy looks for an *unformatted*
NVMe disk (no filesystem, no partitions, not mounted) large enough to hold the ~90 GB **gzipped** planet download. If
found, it partitions it (GPT, one partition), formats it ext4 and mounts it at the download staging dir
(`/data/ofm/linux_host/tmp`). The `.gz` then lands on fast, cheap local NVMe and is **stream-decompressed straight
onto the root/EBS volume** (`unpigz -c`), so the extracted ~165 GB `tiles.btrfs` — the thing the AMI captures — stays
on EBS, while only the throwaway ~90 GB `.gz` lives on the NVMe. Since instance-store NVMe is never part of an AMI,
the download bytes are automatically excluded from the image. Existing/formatted disks (including the root disk) are
never touched, and if no suitable disk is found the download goes onto the versions volume instead.

- `--no-nvme` — disable the NVMe search entirely and download onto the versions volume.
- `--nvme-min-size-gb N` — minimum unformatted disk size to qualify (default `100`, sized for the ~90 GB gzipped
  planet download).

Note: instance-store NVMe is *ephemeral* — it is wiped on stop/start. The fstab entry uses `nofail`, so a later
stop/start (which brings the volume back blank) never blocks boot; a subsequent sync then simply falls back to the
versions volume. The extracted `tiles.btrfs` already lives on EBS, so it survives the stop/start regardless.

**Fast re-bake with unchanged tiles (`--copy-runs-from-host`).** When you re-bake only to pick up new
scripts/config (as after a rebase onto a new upstream) but the **tiles are unchanged**, you do not need to
re-download the planet. Copy the runs directly from a host you already operate over `scp` — minutes instead of
hours:

```
./linux_host/deploy_linux_host.py --config bake --host <IP-ADDRESS> --user ec2-user \
    --copy-runs-from-host 10.0.0.5 --copy-runs-user ec2-user
```

The runs (`/data/ofm/linux_host/versions/<area>/<version>/`, all areas — planet plus the tiny ones) are copied into
place on the new host *before* the sync, so the subsequent `local_versions` sync serves them with no download.
Notes:

- `--copy-runs-from-host` **requires `"local_versions": true`** in the bake config (the deploy refuses otherwise):
  copied runs are served as-is, and without local mode the sync would follow the upstream pointer and re-download.
- `--copy-runs-user` is the SSH user on the **source** host; it defaults to the resolved target SSH user (the
  `user@` in `--host`, else `--user`, else config `ssh_user`, else `ec2-user`).
- The `scp` runs **on the new host** and authenticates to the source using your **forwarded ssh-agent** (agent
  forwarding is enabled automatically). Make sure your local `ssh-agent` holds a key that can reach the source host
  (`ssh-add`) before running the deploy. The source host key is auto-accepted (`StrictHostKeyChecking=accept-new`).

**3. Verify on the box** before snapshotting:

```
curl -sI http://localhost/monaco | sort                                    # expect HTTP/1.1 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/healthz/planet    # expect 204
```

`/healthz/<area>` returns **204** when that area's btrfs is mounted and **503** otherwise — this is the endpoint the
ALB health check should target (see step 5).

**4. Create the AMI.** Optionally stop the instance first for a fully consistent snapshot, then
*Actions → Image and templates → Create image* (or `aws ec2 create-image`). The AMI captures the root EBS volume with
the mounted tiles; the ephemeral instance-store NVMe is excluded automatically, so the throwaway download bytes never
end up in the image.

Optional tidy-up before imaging: if the NVMe download volume was used, the deploy left an fstab entry for it
(`UUID=… /data/ofm/linux_host/tmp ext4 defaults,nofail 0 2`). It is harmless on the fleet — `nofail` means a
`t4g.small` with no such NVMe boots fine and the tiles still mount from the root volume — but you may remove that one
line from `/etc/fstab` before baking to keep the image clean.

**5. Wire up the ALB + Auto Scaling Group.** Boot `t4g.small` instances from the AMI:

- **Launch template:** the baked AMI, instance type `t4g.small`, a security group allowing inbound **80** from the
  ALB's security group.
- **Target group:** protocol HTTP, port 80. Health check path **`/healthz/planet`** (or `/healthz/monaco` if you only
  serve monaco), with **success codes set to `204`** — the default `200` will mark healthy hosts as unhealthy.
- **Auto Scaling Group:** attach it to that target group. New instances come up already holding the tiles, pass the
  `/healthz` check within seconds, and start receiving traffic. TLS is terminated at the ALB, matching the `cert: alb`
  "no local certificates" model (the domain you baked in is used only for `server_name` and the generated
  TileJSON/style URLs).

Because every instance is identical and immutable, scaling out is instant and there is no shared state to coordinate.
To ship a new planet version, bake a fresh AMI and roll the launch template / ASG to it (e.g. an instance refresh).

**6. (Optional) Secure the fleet without baking a secret into the AMI.** A secret should not live in an image
(rotation would force a re-bake, and the AMI could be shared/copied), so the golden AMI is baked **public** — tile-auth
secrets are **not** part of `config.jsonc` and are never present in the image. Each instance is instead secured **at
launch** by handing it the secret through EC2 **user data**. The deploy installs a oneshot systemd unit,
`ofm-tile-auth.service`, ordered *before* `nginx.service` (via `Before=nginx.service`, an ordering-only dependency —
no hard `Requires=`, so it can never block nginx from starting). On every boot, before nginx starts, it reads the
instance user data and applies a `TILE_AUTH_SECRETS` assignment to the runtime state file
(`/data/ofm/linux_host/state/tile_auth_secrets.json`), regenerating the nginx config with the short-lived-token guard
active.

Put a single line in the launch template's **user data** (a bare env line, or the same line inside a `#!/bin/bash`
script — both are recognised):

```
TILE_AUTH_SECRETS=k1:AbC-123_xyz,k2:Def-456_uvw
```

The value uses the exact same `kid:secret,…` format and `[A-Za-z0-9_-]` alphabet described under *Restricting access
with short-lived tokens* below. Rotating secrets is then just editing the launch template's user data and refreshing
the ASG — no new AMI. Notes:

- The unit reads the secret from the instance metadata service (IMDSv2) inside the process, so it never appears on a
  command line or in the AMI. Nothing is logged except the key ids.
- It is **non-destructive** and never blocks nginx. The variable is a tri-state:
  **absent** from user data → leave the existing runtime state untouched (a public AMI stays public; a secured host
  keeps its secret across reboots); **`TILE_AUTH_SECRETS=k1:…`** → enable auth with those secrets;
  **`TILE_AUTH_SECRETS=`** (explicitly empty) → clear the secret and serve public.
- On a non-EC2 host or a transient IMDS hiccup it leaves the state unchanged and still lets nginx start. A *malformed*
  `TILE_AUTH_SECRETS` marks the unit failed (visible in `systemctl status ofm-tile-auth.service`) but nginx still
  comes up from the existing config — fix the value and reboot (or re-run the unit).
- To flip a running fleet between public and secured, either rotate live (step 7) or change the user-data line
  (`TILE_AUTH_SECRETS=…` to secure, `TILE_AUTH_SECRETS=` to go public) and replace the instances (an ASG instance
  refresh) — the user-data change is applied on the next boot.

**7. (Optional) Rotate the secret live, without replacing instances.** Replacing every instance just to change a
secret is slow and expensive. `rotate-tile-auth-secrets.sh` (at the repo root) rotates the secret on the **running**
fleet in seconds: given an authenticated `aws` CLI and SSH access (your local ssh-agent / key), it discovers the
instances registered in a target group, SSHes into each, and runs
`./linux_host/scripts/linux_host.py set-tile-auth-secrets` on the host to rewrite the runtime state file and **reload
nginx gracefully** (SIGHUP — existing connections finish on the old workers, so no requests are dropped). The secret
travels over the SSH stdin pipe, so it is never on any command line.

```
# rotate: keep the old kid AND add the new one, so tokens signed with either stay valid
TILE_AUTH_SECRETS='k1:old-secret,k2:new-secret' \
  ./rotate-tile-auth-secrets.sh --target-group ofm-tiles --region eu-central-1

# ...roll the app over to signing with k2, wait for all k1 tokens to expire, then drop k1:
TILE_AUTH_SECRETS='k2:new-secret' \
  ./rotate-tile-auth-secrets.sh --target-group ofm-tiles --region eu-central-1
```

Useful flags: `--target-group` accepts a name or a full ARN; `--secrets-file <path>` (or an interactive prompt) as an
alternative to the env var; `--clear` to make the fleet public; `--use-private-ip`, `--ssh-user`, `--ssh-option` (e.g.
a `ProxyJump` bastion) for connectivity; `--dry-run` to just list the discovered hosts; `-y` to skip the confirmation.
Run `./rotate-tile-auth-secrets.sh --help` for the full list.

> **This changes the running config only.** On the next reboot, `ofm-tile-auth.service` re-reads the instance's EC2
> user data. So for a *durable* rotation, also update the launch template's user-data line (step 6) — then the live
> script secures the current instances immediately, and any instance that reboots or is newly launched picks up the
> same secret. New instances that launch *between* the two changes get whatever the launch template currently has;
> keep the old kid listed until you are sure no live token still uses it.

---

#### Restricting access with short-lived tokens (optional)

By default every location is public. If you serve tiles to a single browser application and want to
keep casual scrapers out **without breaking caching**, you can require a short-lived, signed access
token on the OFM data locations. The token is validated by nginx's built-in
[`secure_link`](https://nginx.org/en/docs/http/ngx_http_secure_link_module.html) module (an MD5 keyed
hash), and — crucially — it travels in **request headers**, not in the URL, so tile URLs stay
byte-identical and fully cacheable by browsers, ALBs and CDNs.

Tile-auth secrets are **runtime state**, not part of `config.jsonc`. They live in
`/data/ofm/linux_host/state/tile_auth_secrets.json` on the host and are applied with a comma-separated list of
`kid:secret` pairs, mapping a short key id (`kid`) to a secret:

```
TILE_AUTH_SECRETS=k1:AbC-123_xyz,k2:Def-456_uvw
```

There are three ways to apply them, all reading the value from **stdin** (so it never lands on a process list) and
all reloading nginx gracefully afterwards:

- **On a single running host** (over SSH):

  ```
  printf 'k1:AbC-123_xyz,k2:Def-456_uvw' | \
    sudo ./linux_host/scripts/linux_host.py set-tile-auth-secrets
  ```

  Add `--clear` (with empty input) to make the host public again.
- **A whole running ASG fleet at once**: `rotate-tile-auth-secrets.sh` (see *Rotate the secret live* above).
- **At boot from EC2 user data**: `ofm-tile-auth.service` (see *Secure the fleet without baking a secret into the
  AMI* above).

**Secret format (hard rule, no escaping).** The comma separates entries and the first colon
separates a kid from its secret, so **both kids and secrets must match the URL-safe base64url
alphabet `[A-Za-z0-9_-]`** — no commas, colons, spaces or quotes inside a secret. A comma in a
secret would be parsed as an entry boundary; there is deliberately no escaping mechanism.
Generate secrets accordingly, e.g.:

```
openssl rand -base64 48 | tr '+/' '-_' | tr -d '='
```

The **same** `kid:secret,…` string is configured verbatim on the application side (git-sail
system property `map.provider.tileserver.auth.secrets`), so both ends share one secret set and
the same alphabet constraint. A malformed value is refused with a clear message rather than
shipping a broken map.

Applying secrets makes the nginx generator:

- write an http-level `map $http_x_ofm_kid $ofm_secret { ... }` to
  `/data/nginx/config/ofm_secure_link.conf`, selecting the signing secret from the client's
  `X-OFM-Kid` header;
- emit the server-level directives
  `secure_link $http_x_ofm_md5,$http_x_ofm_expires;` and
  `secure_link_md5 "$secure_link_expires $ofm_secret";`; and
- add, to every OFM data location (area JSON/tilejson, PBF tiles, wildcard, `/styles/`, `/fonts/`,
  `/sprites/`, `/natural_earth/`), a guard that returns **401** unless `$secure_link` is `1`
  (`""` = missing/malformed token, `"0"` = bad or expired). `@empty_tile`, `/healthz/{area}`,
  `location = /` and OPTIONS preflights are intentionally left open.

The client must send three request headers with every tile-server request:

| Header         | Value                                                                              |
|----------------|------------------------------------------------------------------------------------|
| `X-OFM-Md5`     | `base64url_nopad( md5( "<expires> <secret>" ) )` (no padding, `+/`→`-_`)           |
| `X-OFM-Expires` | token expiry as seconds since the epoch (decimal)                                   |
| `X-OFM-Kid`     | the key id whose secret produced the signature (e.g. `k1`)                          |

The signed string is exactly `"<expires> <secret>"` — the decimal expiry, a single space, then the
secret — matching `secure_link_md5 "$secure_link_expires $ofm_secret"`. In shell this is
`printf '%s %s' "$EXPIRES" "$SECRET" | openssl md5 -binary | openssl base64 | tr '+/' '-_' | tr -d '='`.

The 401 responses carry `Access-Control-Allow-Origin: *` so a browser on a *different* origin than the
tile server (e.g. app on `www.example.com`, tiles on `maptiles.example.com`) can read the status and
refresh its token rather than seeing an opaque CORS error.

**Rotation** is zero-downtime: add a new `kid` (e.g. `k3:...`) alongside the existing ones, apply the new value,
start signing with it on the application side, and keep the old ids listed until every token signed with them has
expired, then remove them. Clear the secrets to return to fully public serving. For a running ASG fleet, rotate live
with `rotate-tile-auth-secrets.sh` (see *Rotate the secret live* in the golden-AMI section above).

**Golden AMIs / Auto Scaling Groups.** Do **not** bake the secret into an AMI. Bake the image **public** (no
tile-auth secrets) and instead hand the secret to each instance at launch via EC2 user data; the
`ofm-tile-auth.service` installed by the deploy applies it before nginx starts. See *Secure the fleet without baking a
secret into the AMI* under the golden-AMI section above.

#### Deploy tilegen server (optional)

If you have a really beefy machine (see above) and you really want to generate tiles yourself:

Copy the tilegen sample config to a named config first:

```
cp config/tilegen/config.sample.jsonc config/tilegen/self-hosted.jsonc
```

Set `cron` to `true` only if this host should run automated tile builds, uploads, version publication, and index refreshes. Then deploy using the config name that maps to `config/tilegen/self-hosted.jsonc`:

```
./tilegen/deploy_tilegen.py --config self-hosted --host <IP-ADDRESS>
```

`--host` takes `[user@]host` and is authoritative (the host need not be in the config's `hosts`); omit it to fall
back to a single `hosts` entry. The same `--user`, `--port`, `ssh_user` (config), `SSH_PASSWD` and `SUDO_PASSWD`
options from the linux_host deploy also work here. Each deployment installs or removes the tilegen cron job according
to the config.

A normal deployment refuses to make any server change if a `make-tiles` build is running.

Reinstall stops all tilegen commands and their child processes, verifies that they stopped, unmounts and verifies filesystems below `/data/ofm`, and then removes `/data/ofm`.

Trigger a run manually over SSH as the `ofm` runtime user:

```
cd /data/ofm/src && sudo -u ofm env PYTHONUNBUFFERED=1 ./tilegen/scripts/tilegen.py make-tiles planet --upload
```

Running as `ofm` keeps manual and scheduled build files under the same ownership.

For a quick smoke test, use `monaco` instead of `planet`. It's recommended to use tmux or similar, as a full planet run can take days to complete.
