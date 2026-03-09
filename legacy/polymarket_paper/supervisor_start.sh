#!/bin/bash
set -euo pipefail
exec /home/linuxbrew/.linuxbrew/opt/supervisor/bin/supervisord -c /data/.openclaw/workspace/polymarket_paper/supervisord.conf
