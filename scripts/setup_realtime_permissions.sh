#!/usr/bin/env bash
set -euo pipefail

USER_NAME="${1:-andy}"
LIMITS_FILE="/etc/security/limits.d/99-realtime.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo $0 ${USER_NAME}" >&2
  exit 1
fi

groupadd -f realtime
usermod -aG realtime "${USER_NAME}"

cat > "${LIMITS_FILE}" <<'EOF'
@realtime soft rtprio 99
@realtime soft priority 99
@realtime soft memlock 102400
@realtime hard rtprio 99
@realtime hard priority 99
@realtime hard memlock 102400
EOF

echo "Configured realtime group and limits for ${USER_NAME}."
echo "Log out/in or reboot, then check:"
echo "  groups"
echo "  ulimit -r"
