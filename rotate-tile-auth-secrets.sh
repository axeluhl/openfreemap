#!/usr/bin/env bash
#
# Live-rotate the OpenFreeMap tile-auth secret across a running fleet, without
# replacing any instances.
#
# It discovers the tile-server instances registered in an ALB target group
# (by name or ARN) via the authenticated `aws` CLI, then SSHes into each one
# (using your local ssh-agent / key) and runs, on the host:
#
#     sudo http_host.py set-tile-auth-secrets   # reads the value from stdin
#
# which rewrites /data/ofm/config/config.json and reloads nginx *gracefully*
# (SIGHUP: existing connections keep being served by the old workers). The new
# secret string travels over the SSH stdin pipe, so it never appears on any
# local or remote command line / process list.
#
# Typical rotation: pass the OLD *and* NEW kids together (e.g.
# "k1:old,k2:new"), roll the application over to signing with the new kid, wait
# for every token signed with the old kid to expire, then run again with only
# the new kid to drop the old one.
#
# NOTE: this updates the *running* config only. On the next reboot, each
# instance re-reads its EC2 user data via ofm-tile-auth.service. For a durable
# rotation, also update the launch template's user data (see
# docs/self_hosting.md) so newly launched / rebooted instances match.
#
# Requirements: bash, an authenticated `aws` CLI (elasticloadbalancing +
# ec2:DescribeInstances), `ssh`, and network/SSH reachability to the instances.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: rotate-tile-auth-secrets.sh --target-group <name|arn> [options]

Required:
  --target-group <name|arn>   ALB target group holding the tile-server instances.
                              Accepts a plain name or a full TargetGroupArn.

Secret source (choose one; if none given, you are prompted):
  --secrets-file <path>       Read the "kid:secret,..." value from this file.
  (env) TILE_AUTH_SECRETS      Read the value from this environment variable.
  --clear                     Clear the secret (make the fleet PUBLIC). No value needed.

Options:
  --region <region>           AWS region (else your aws CLI default / env).
  --ssh-user <user>           SSH user (default: ec2-user).
  --use-private-ip            SSH to private IPs (default: public IP, falling
                              back to private if no public IP).
  --ssh-option <opt>          Extra ssh -o option (repeatable), e.g.
                              --ssh-option "ProxyJump=bastion".
  --include-unhealthy         Also target instances not currently 'healthy'
                              (default: only healthy + initial targets).
  --dry-run                   Only discover and list the target hosts; do not
                              connect or change anything.
  -y, --yes                   Do not ask for confirmation before applying.
  -h, --help                  Show this help.

Examples:
  TILE_AUTH_SECRETS='k1:AbC-123_xyz,k2:Def-456_uvw' \
    ./rotate-tile-auth-secrets.sh --target-group ofm-tiles --region eu-central-1

  ./rotate-tile-auth-secrets.sh --target-group ofm-tiles --secrets-file new-secrets.txt
EOF
    exit "${1:-2}"
}

# ---- defaults ---------------------------------------------------------------
TARGET_GROUP=""
REGION=""
SSH_USER="ec2-user"
USE_PRIVATE_IP=0
SECRETS_FILE=""
CLEAR=0
INCLUDE_UNHEALTHY=0
DRY_RUN=0
ASSUME_YES=0
SSH_EXTRA_OPTS=()

# ---- arg parsing ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-group) TARGET_GROUP="${2:?}"; shift 2 ;;
        --region)       REGION="${2:?}"; shift 2 ;;
        --ssh-user)     SSH_USER="${2:?}"; shift 2 ;;
        --use-private-ip) USE_PRIVATE_IP=1; shift ;;
        --secrets-file) SECRETS_FILE="${2:?}"; shift 2 ;;
        --clear)        CLEAR=1; shift ;;
        --ssh-option)   SSH_EXTRA_OPTS+=("-o" "${2:?}"); shift 2 ;;
        --include-unhealthy) INCLUDE_UNHEALTHY=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -y|--yes)       ASSUME_YES=1; shift ;;
        -h|--help)      usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 2 ;;
    esac
done

[[ -n "$TARGET_GROUP" ]] || { echo "Error: --target-group is required." >&2; usage 2; }
command -v aws >/dev/null 2>&1 || { echo "Error: 'aws' CLI not found on PATH." >&2; exit 1; }
command -v ssh >/dev/null 2>&1 || { echo "Error: 'ssh' not found on PATH." >&2; exit 1; }

AWS_REGION_ARGS=()
[[ -n "$REGION" ]] && AWS_REGION_ARGS=(--region "$REGION")

# ---- resolve the secret value ----------------------------------------------
# SECRETS is the raw "kid:secret,..." string. Empty string means "clear".
SECRETS=""
if [[ "$CLEAR" -eq 1 ]]; then
    SECRETS=""
elif [[ -n "$SECRETS_FILE" ]]; then
    [[ -r "$SECRETS_FILE" ]] || { echo "Error: cannot read --secrets-file '$SECRETS_FILE'." >&2; exit 1; }
    SECRETS="$(<"$SECRETS_FILE")"
elif [[ -n "${TILE_AUTH_SECRETS:-}" ]]; then
    SECRETS="$TILE_AUTH_SECRETS"
else
    # Prompt without echoing to the terminal.
    read -r -s -p "New TILE_AUTH_SECRETS (kid:secret,...): " SECRETS
    echo
fi
# Trim surrounding whitespace/newlines.
SECRETS="$(printf '%s' "$SECRETS" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

if [[ "$CLEAR" -ne 1 && -z "$SECRETS" ]]; then
    echo "Error: empty secret. Pass --clear to make the fleet public, or provide a value." >&2
    exit 1
fi

# ---- discover the target group + instances ---------------------------------
if [[ "$TARGET_GROUP" == arn:* ]]; then
    TG_ARN="$TARGET_GROUP"
else
    echo "Resolving target group '$TARGET_GROUP' ..." >&2
    TG_ARN="$(aws "${AWS_REGION_ARGS[@]}" elbv2 describe-target-groups \
        --names "$TARGET_GROUP" \
        --query 'TargetGroups[0].TargetGroupArn' --output text)"
    [[ -n "$TG_ARN" && "$TG_ARN" != "None" ]] || { echo "Error: target group '$TARGET_GROUP' not found." >&2; exit 1; }
fi

TARGET_TYPE="$(aws "${AWS_REGION_ARGS[@]}" elbv2 describe-target-groups \
    --target-group-arns "$TG_ARN" \
    --query 'TargetGroups[0].TargetType' --output text)"

echo "Target group: $TG_ARN (target type: $TARGET_TYPE)" >&2

# Collect target ids, optionally filtering by health state.
if [[ "$INCLUDE_UNHEALTHY" -eq 1 ]]; then
    HEALTH_QUERY='TargetHealthDescriptions[].Target.Id'
else
    HEALTH_QUERY="TargetHealthDescriptions[?TargetHealth.State=='healthy' || TargetHealth.State=='initial'].Target.Id"
fi

mapfile -t TARGET_IDS < <(aws "${AWS_REGION_ARGS[@]}" elbv2 describe-target-health \
    --target-group-arn "$TG_ARN" \
    --query "$HEALTH_QUERY" --output text | tr '\t' '\n' | sed '/^$/d' | sort -u)

[[ "${#TARGET_IDS[@]}" -gt 0 ]] || { echo "Error: no targets found in the group." >&2; exit 1; }

# Resolve each target to an SSH address.
declare -a HOSTS=()
if [[ "$TARGET_TYPE" == "instance" ]]; then
    # One describe-instances call for all ids; pick public IP (fallback private)
    # or private IP when --use-private-ip was given.
    while read -r iid pub priv; do
        [[ -z "$iid" ]] && continue
        if [[ "$USE_PRIVATE_IP" -eq 1 ]]; then
            addr="$priv"
        else
            addr="$pub"; [[ -z "$addr" || "$addr" == "None" ]] && addr="$priv"
        fi
        if [[ -z "$addr" || "$addr" == "None" ]]; then
            echo "  warning: no usable IP for instance $iid, skipping" >&2
            continue
        fi
        HOSTS+=("$iid=$addr")
    done < <(aws "${AWS_REGION_ARGS[@]}" ec2 describe-instances \
        --instance-ids "${TARGET_IDS[@]}" \
        --query 'Reservations[].Instances[].[InstanceId,PublicIpAddress,PrivateIpAddress]' \
        --output text)
else
    # 'ip' target type: the target ids are IP addresses already.
    for ip in "${TARGET_IDS[@]}"; do
        HOSTS+=("$ip=$ip")
    done
fi

[[ "${#HOSTS[@]}" -gt 0 ]] || { echo "Error: could not resolve any SSH addresses." >&2; exit 1; }

echo "Discovered ${#HOSTS[@]} host(s):" >&2
for h in "${HOSTS[@]}"; do echo "  - ${h%%=*}  ->  ${h##*=}" >&2; done

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "(dry run) not connecting to any host." >&2
    exit 0
fi

# ---- confirm ----------------------------------------------------------------
if [[ "$CLEAR" -eq 1 ]]; then
    ACTION="CLEAR the tile-auth secret (make the fleet PUBLIC)"
else
    # Show only the kids, never the secrets.
    KIDS="$(printf '%s' "$SECRETS" | tr ',' '\n' | sed -e 's/:.*$//' | paste -sd, -)"
    ACTION="set tile-auth secret kid(s): $KIDS"
fi
echo "About to $ACTION on ${#HOSTS[@]} host(s) as SSH user '$SSH_USER'." >&2
if [[ "$ASSUME_YES" -ne 1 ]]; then
    read -r -p "Proceed? [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted." >&2; exit 1; }
fi

# ---- apply per host ---------------------------------------------------------
REMOTE_CMD="sudo /data/ofm/venv/bin/python /data/ofm/http_host/bin/http_host.py set-tile-auth-secrets"
[[ "$CLEAR" -eq 1 ]] && REMOTE_CMD+=" --clear"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
[[ "${#SSH_EXTRA_OPTS[@]}" -gt 0 ]] && SSH_OPTS+=("${SSH_EXTRA_OPTS[@]}")

ok=0
fail=0
failed_hosts=()
for h in "${HOSTS[@]}"; do
    id="${h%%=*}"
    addr="${h##*=}"
    echo "==> $id ($addr)" >&2
    # Pipe the secret over stdin so it is never on the remote command line.
    # 'sudo' inherits stdin, and the python command reads it. REMOTE_CMD is a
    # fixed local constant (no untrusted data), so client-side expansion is fine.
    # shellcheck disable=SC2029
    if printf '%s' "$SECRETS" | ssh "${SSH_OPTS[@]}" "${SSH_USER}@${addr}" "$REMOTE_CMD"; then
        ok=$((ok + 1))
    else
        echo "    FAILED on $id ($addr)" >&2
        fail=$((fail + 1))
        failed_hosts+=("$id ($addr)")
    fi
done

echo "----------------------------------------" >&2
echo "Done: $ok succeeded, $fail failed (of ${#HOSTS[@]})." >&2
if [[ "$fail" -gt 0 ]]; then
    printf '  failed: %s\n' "${failed_hosts[@]}" >&2
    exit 1
fi
