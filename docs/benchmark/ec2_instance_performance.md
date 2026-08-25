# AWS EC2 instance performance for http-host

*Background read on sizing an http-host tile server on AWS EC2. Load testing done 2026-08, eu-west-1.*

## TL;DR

For serving OpenFreeMap vector tiles, an http-host is **network-egress-bound, not CPU-bound**.
Every instance type tested sat largely CPU-idle at its throughput ceiling; the binding
resource is the ENA outbound-bandwidth allowance. Consequently a small, cheap burstable
instance (**t4g.small**) is the sweet spot — it sustained *more* egress than an instance
~9.5× more expensive, at a fraction of the cost, and it images cleanly into an AMI for
auto-scaling.

## Workload

- Tiles are small pre-compressed (gzip) protobuf blobs. Measured **average on-wire size ≈ 50,493 bytes**.
- nginx serves them straight from a mounted btrfs image (`sendfile`), so per-request CPU is tiny.
- Realistic production peak for the reference deployment (~10 simultaneous sailing events)
  is in the low-thousands of requests/s — far below any instance's ceiling.

## Method

- Load generated from an in-region EC2 generator (aarch64, 8 vCPU) hitting the tile host
  directly on port 80 (generator placed in the ALB security group).
- Driver: [`vegeta`](https://github.com/tsenart/vegeta) in **closed-loop** mode
  (`-rate=0 -max-workers=N`), i.e. N concurrent workers firing back-to-back requests.
  Request headers: `Host: maptiles.sapsailing.com`, `Accept-Encoding: gzip`, keep-alive on.
- Ground truth on the target: NIC `tx_bytes` delta (→ Gbps) plus the ENA
  `bw_out_allowance_exceeded` counter delta, sampled every few seconds during each run.
- Each ceiling number is from a **sustained ≥190 s run** so the network burst bucket is
  depleted and the reading reflects the *baseline* allowance, not burst.

> **Pitfall — open-loop vs closed-loop.** A fixed-rate (`-rate=R`) vegeta run *above*
> server capacity causes connection pile-up, SYN retransmits and 30 s timeouts, collapsing
> throughput to a misleadingly low number (a "death spiral"). This produced a bogus
> "~8,500 rps plateau" in early runs. Closed-loop (`-rate=0 -max-workers`) avoids it: the
> same c6gd then cleanly served ~12,500 rps. **Always measure with a closed-loop tool.**
>
> Also set `vegeta -max-body 0` so it doesn't buffer response bodies and fill the
> generator's disk; measure tile size separately with `curl -w %{size_download}`.

## Results

Sustained egress ceiling (steady for the full run, ENA throttle active throughout):

| Instance    | vCPU / RAM   | Sustained egress | ~ tiles/s @50 KB | Notes                          |
|-------------|--------------|------------------|------------------|--------------------------------|
| t3.small    | 2 / 2 GB     | **5.27 Gbps**    | ~13,000          | x86, burstable                 |
| t4g.small   | 2 / 2 GB     | **5.25 Gbps**    | ~13,000          | ARM (Graviton), burstable      |
| c6gd.xlarge | 4 / 8 GB     | **4.15 Gbps**    | ~10,300          | ARM, local NVMe instance store |

All three were **ENA-egress-throttled** at these rates (the `bw_out_allowance_exceeded`
counter incremented continuously) while CPU stayed largely idle. The ceilings held rock
steady for 4+ minutes, so these are baseline allowances, not burst.

### The counter-intuitive part

The two **cheapest** instances sustained **~27% more egress** than the one that is
**~9.5x more expensive**. Moreover the small instances did so **cross-AZ** (the generator
was in the c6gd's AZ), so the network path *favoured* the c6gd - yet it still came out
lower. The gap is therefore a genuine per-instance ENA egress allowance, not a
path/placement artifact. Bigger compute does **not** buy more tile-serving capacity here.

## Cost (eu-west-1, on-demand)

Disk: gp3 at **$0.0836 / GB-month**. The root volume uses ~159 GB, so **200 GB** is
comfortable (300 GB is over-provisioned). 200 GB gp3 = **$16.72 / month**.

| Instance    | Compute / mo | Disk (200 GB gp3) / mo | **Total / mo** | **$ / sustained Gbps-mo** |
|-------------|--------------|------------------------|----------------|---------------------------|
| t4g.small   | $13.43       | $16.72                 | **$30.15**     | ~$5.7                     |
| t3.small    | $15.18       | $16.72                 | **$31.90**     | ~$6.1                     |
| c6gd.xlarge | $127.31      | $16.72                 | **$144.03**    | ~$34.7                    |

At 200 GB, disk is ~55% of a small host's monthly bill. A c6gd.xlarge costs **~4.8x** a
t4g.small in total, for **less** sustained egress.

*(c6gd's price bundles a free ~237 GB local NVMe instance store. You could move tile data
there and shrink the gp3 root, but that storage is ephemeral - wiped on stop, needs a
re-sync on every launch - and it does nothing for the lower egress cap. It does not rescue
the economics.)*

## Recommendation

- **Use t4g.small as the http-host unit.** Cheapest, highest sustained egress, ARM/Graviton
  efficiency, best tail latency-per-dollar.
- **Scale horizontally, not vertically.** Because bandwidth is the only real limit and it
  is per-instance, N x t4g.small behind the ALB gives N x the aggregate egress for less
  money than one big instance, while also spreading across AZs for HA.
- **Bake an AMI.** A t4g.small with OS, venv, nginx config and tiles baked into a gp3-root
  AMI boots in about a minute with everything deployed, ideal for an Auto Scaling Group
  that grows/shrinks the fleet on demand.
- The **c6gd.xlarge is not worth it** for pure http-host: more expensive, lower egress, and
  its main perk (local NVMe) is ephemeral and irrelevant to the bottleneck.

## Caveats

- Numbers are from eu-west-1 in 2026-08; ENA allowances and prices vary by region,
  instance generation, and over time. Re-measure for your own region and instance choice.
- Tile mix matters: sizes vary widely by zoom and area. We used a Zipf-weighted mix over
  ~10 sailing venues; average on-wire size was ~50 KB gzipped. A different mix shifts the
  tiles/s numbers but not the "egress-bound" conclusion.
- Always drive with a closed-loop benchmark and confirm the bottleneck by watching the
  server's NIC TX and ENA `bw_out_allowance_exceeded` counter, not just the client's rps.
