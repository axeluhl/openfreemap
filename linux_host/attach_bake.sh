#!/usr/bin/env bash
#
# Re-attach to the detached tmux session that `deploy_linux_host.py --bake` runs the bake
# sync in, after an SSH connection to the bake box is lost. The bake download keeps running
# on the box inside tmux across a disconnect (see run_linux_host_sync_baked); this just
# reconnects you to watch it.
#
# Usage:
#   ./linux_host/attach_bake.sh <host-or-ip> [ssh-user]      # attach to the tmux session
#   ./linux_host/attach_bake.sh --tail <host-or-ip> [user]   # follow the log instead of attaching
#
# ssh-user defaults to ec2-user. Detach from an attached session with Ctrl-b d (do NOT press
# Ctrl-c — that would signal the running sync inside the pane).

set -euo pipefail

# Must match run_linux_host_sync_baked / linux_host_deploy_config.
SESSION='ofm_linux_host_bake'
LOG_FILE='/data/ofm/linux_host/logs/sync.log'

usage() {
    sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

mode='attach'
if [[ "${1:-}" == '--tail' ]]; then
    mode='tail'
    shift
elif [[ "${1:-}" == '-h' || "${1:-}" == '--help' ]]; then
    usage 0
fi

host="${1:-}"
user="${2:-ec2-user}"
if [[ -z "$host" ]]; then
    echo "error: missing host/IP" >&2
    usage 1 >&2
fi

target="${user}@${host}"

if [[ "$mode" == 'tail' ]]; then
    echo "Following ${LOG_FILE} on ${target} (Ctrl-c to stop; the sync is unaffected)..."
    exec ssh -t "$target" "sudo tail -f $(printf '%q' "$LOG_FILE")"
fi

# -t forces a pty so tmux attaches interactively over SSH.
echo "Attaching to tmux session '${SESSION}' on ${target} (detach with Ctrl-b d, NOT Ctrl-c)..."
if ssh -t "$target" "sudo tmux attach -t $(printf '%q' "$SESSION")"; then
    exit 0
fi

# tmux exits non-zero when the session does not exist: the bake either never started here or
# already finished (a finished phase ends its session). Point at the log so the operator can
# still see the outcome instead of a bare "no such session".
cat >&2 <<EOF

No tmux session '${SESSION}' on ${target}.
That means the bake sync is not currently running there -- it either already finished
(each phase ends its session when it returns) or was never started on this host.
Check the result in the log:

  $0 --tail ${host} ${user}
EOF
exit 1
