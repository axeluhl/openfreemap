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

##### Baking a golden AMI for an Auto Scaling Group behind an ALB

This is the recommended way to run a fleet: build the tiles **once** on a big Graviton box with local NVMe, snapshot
its root volume into an AMI, then boot cheap `t4g.small` instances from that AMI. Each instance comes up already
holding the planet tiles on its own root disk — no per-instance 90 GB download — so it can serve within seconds of
boot and slot straight into an ALB target group managed by an Auto Scaling Group.

The whole architecture stays **arm64**: the bake instance is Graviton, so the AMI is arm64, so it runs on `t4g.small`.
Do not bake on an x86 instance.

**1. Launch the bake instance.** From a clean **Amazon Linux 2023 (arm64)** image:

- Instance type **`m6gd.large`** (2 vCPU, 8 GB RAM, 1×118 GB instance-store NVMe).
- A single **200 GB gp3 root** EBS volume — same size the `t4g.small` fleet will run, so the AMI is sized right.
  During the bake, temporarily raise the root volume's gp3 throughput (e.g. to 500–1000 MB/s) so the ~170 GB
  decompress-to-EBS write is fast; runtime reads on `t4g.small` are unaffected by that setting.
- Security group: inbound SSH (22) from your workstation, and outbound HTTPS (443) so it can download the tiles.
- The 118 GB NVMe is left **unformatted** — the deploy detects it and uses it to stage the download (see *Local NVMe
  for the btrfs download* above). It is instance-store, so it is **never captured in the AMI**; only the extracted
  `tiles.btrfs` on the root/EBS volume is.

**2. Run the deploy** from your workstation (with this repo and a filled-in `config/.env`, `SKIP_PLANET=false`):

```
./init-server.py http-host-static ec2-user@<IP-ADDRESS> --domain maps.example.com
```

Then wait. The NVMe is partitioned/formatted/mounted at `runs/_tmp`, the ~90 GB `.gz` downloads onto it and is
decompressed straight onto the 200 GB root volume. On `m6gd.large` this is the long step (download + `unpigz`); the
"Download aborted" lines from CloudFlare in the meantime are harmless.

Use `http-host-static` (not `http-host-autoupdate`): a golden AMI should serve a **fixed, immutable** version. You do
not want each auto-scaled instance re-downloading tiles or drifting to a different version at boot. (If you ever do
want the fleet to self-update, that is a different design — bake with `http-host-autoupdate` and accept that each
instance pulls new tiles on its own schedule.)

**3. Verify on the box** before snapshotting:

```
curl -sI http://localhost/monaco | sort            # expect HTTP/1.1 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/healthz/planet   # expect 204
```

`/healthz/<area>` returns **204** when that area's btrfs is mounted and **503** otherwise — this is the endpoint the
ALB health check should target (see step 5).

**4. Create the AMI.** Optionally stop the instance first for a fully consistent snapshot, then
*Actions → Image and templates → Create image* (or `aws ec2 create-image`). The AMI captures only the root EBS
volume; the ephemeral NVMe is excluded automatically, so the throwaway download bytes never end up in the image.

Optional tidy-up before imaging: the deploy left an fstab entry for the NVMe download volume
(`UUID=… /data/ofm/http_host/runs/_tmp ext4 defaults,nofail 0 2`). It is harmless on the fleet — `nofail` means a
`t4g.small` with no such NVMe boots fine and the tiles still mount from the root volume — but you may remove that one
line from `/etc/fstab` before baking to keep the image clean.

**5. Wire up the ALB + Auto Scaling Group.** Boot `t4g.small` instances from the AMI:

- **Launch template:** the baked AMI, instance type `t4g.small`, a security group allowing inbound **80** from the
  ALB's security group.
- **Target group:** protocol HTTP, port 80. Health check path **`/healthz/planet`** (or `/healthz/monaco` if you only
  serve monaco), with **success codes set to `204`** — the default `200` will mark healthy hosts as unhealthy.
- **Auto Scaling Group:** attach it to that target group. New instances come up already holding the tiles, pass the
  `/healthz` check within seconds, and start receiving traffic. TLS is terminated at the ALB, matching this repo's
  "no local certificates" assumption (the `--domain` you baked in is used only for `server_name` and the generated
  TileJSON/style URLs).

Because every instance is identical and immutable, scaling out is instant and there is no shared state to coordinate.
To ship a new planet version, bake a fresh AMI and roll the launch template / ASG to it (e.g. an instance refresh).

**6. (Optional) Secure the fleet without baking a secret into the AMI.** A secret should not live in an image
(rotation would force a re-bake, and the AMI could be shared/copied), so the golden AMI is baked **public** — leave
`TILE_AUTH_SECRETS` empty in `config/.env` when you bake. Each instance is instead secured **at launch** by handing it
the secret through EC2 **user data**. The deploy installs a oneshot systemd unit, `ofm-tile-auth.service`, ordered
*before* `nginx.service` (via `Before=nginx.service`, an ordering-only dependency — no hard `Requires=`, so it can
never block nginx from starting). On every boot, before nginx starts, it reads the instance user data and applies a
`TILE_AUTH_SECRETS` assignment to `config.json`, regenerating the nginx config with the short-lived-token guard active.

Put a single line in the launch template's **user data** (a bare env line, or the same line inside a `#!/bin/bash`
script — both are recognised):

```
TILE_AUTH_SECRETS=k1:AbC-123_xyz,k2:Def-456_uvw
```

The value uses the exact same `kid:secret,…` format and `[A-Za-z0-9_-]` alphabet as the `.env` variable (see
*Restricting access with short-lived tokens* below). Rotating secrets is then just editing the launch template's user
data and refreshing the ASG — no new AMI. Notes:

- The unit reads the secret from the instance metadata service (IMDSv2) inside the process, so it never appears on a
  command line or in the AMI. Nothing is logged except the key ids.
- It is **non-destructive** and never blocks nginx. The variable is a tri-state:
  **absent** from user data → leave the existing config untouched (a public AMI stays public; a `.env`-secured host
  keeps its secret across reboots); **`TILE_AUTH_SECRETS=k1:…`** → enable auth with those secrets;
  **`TILE_AUTH_SECRETS=`** (explicitly empty) → clear the secret and serve public.
- On a non-EC2 host or a transient IMDS hiccup it leaves the config unchanged and still lets nginx start. A *malformed*
  `TILE_AUTH_SECRETS` marks the unit failed (visible in `systemctl status ofm-tile-auth.service`) but nginx still comes
  up from the existing config — fix the value and reboot (or re-run the unit).
- To flip a running fleet between public and secured, either rotate live (step 7) or change the user-data line
  (`TILE_AUTH_SECRETS=…` to secure, `TILE_AUTH_SECRETS=` to go public) and replace the instances (an ASG instance
  refresh) — the user-data change is applied on the next boot.

**7. (Optional) Rotate the secret live, without replacing instances.** Replacing every instance just to change a
secret is slow and expensive. `rotate-tile-auth-secrets.sh` (at the repo root) rotates the secret on the **running**
fleet in seconds: given an authenticated `aws` CLI and SSH access (your local ssh-agent / key), it discovers the
instances registered in a target group, SSHes into each, and runs `http_host.py set-tile-auth-secrets` on the host to
rewrite `config.json` and **reload nginx gracefully** (SIGHUP — existing connections finish on the old workers, so no
requests are dropped). The secret travels over the SSH stdin pipe, so it is never on any command line.

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

#### Restricting access with short-lived tokens (optional)

By default every location is public. If you serve tiles to a single browser application and want to
keep casual scrapers out **without breaking caching**, you can require a short-lived, signed access
token on the OFM data locations. The token is validated by nginx's built-in
[`secure_link`](https://nginx.org/en/docs/http/ngx_http_secure_link_module.html) module (an MD5 keyed
hash), and — crucially — it travels in **request headers**, not in the URL, so tile URLs stay
byte-identical and fully cacheable by browsers, ALBs and CDNs.

Enable it by setting `TILE_AUTH_SECRETS` in your `config/.env` **before deploying** — a
comma-separated list of `kid:secret` pairs, mapping a short key id (`kid`) to a secret:

```
TILE_AUTH_SECRETS=k1:AbC-123_xyz,k2:Def-456_uvw
```

> **Do not hand-edit `config.json` on the host.** `/data/ofm/config/config.json` is
> regenerated from `.env` by `upload_config_json()` on every `http-host-static` run (and on
> any `http-host-sync --domain` run), so a `tile_auth_secrets` object added there by hand is
> overwritten. `.env` is the single source of truth; the deploy writes the corresponding
> `tile_auth_secrets` object into `config.json` for you.

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
the same alphabet constraint. A malformed value aborts the deploy with a clear message rather
than shipping a broken map.

On the next `http-host-static` / `http-host-sync` run this makes `write_nginx_config()`:

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

**Rotation** is zero-downtime: append a new `kid` (e.g. `k3:...`) to `TILE_AUTH_SECRETS`, redeploy,
start signing with it on the application side, and keep the old ids listed until every token signed
with them has expired, then remove them. Clear `TILE_AUTH_SECRETS` (and redeploy) to return to fully
public serving. For a running ASG fleet you don't have to redeploy at all — rotate live with
`rotate-tile-auth-secrets.sh` (see *Rotate the secret live* in the golden-AMI section above).

**Golden AMIs / Auto Scaling Groups.** Do **not** bake the secret into an AMI. Leave `TILE_AUTH_SECRETS`
empty when baking (public image) and instead hand the secret to each instance at launch via EC2 user
data; the `ofm-tile-auth.service` installed by the deploy applies it before nginx starts. See *Secure the
fleet without baking a secret into the AMI* under the golden-AMI section above.

#### Deploy tile-gen server (optional)
If you have a really beefy machine (see above) and you really want to generate tiles yourself, you can run `./init-server.py tile-gen HOSTNAME`.

Trigger a run manually, by running

```
sudo /data/ofm/venv/bin/python -u /data/ofm/tile_gen/bin/tile_gen.py make-tiles planet
```

It's recommended to use tmux or similar, as it can take days to complete.
