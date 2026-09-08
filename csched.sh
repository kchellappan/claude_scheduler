#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec python3 -m csched "$@"
