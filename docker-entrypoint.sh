#!/bin/sh
set -eu

# Bind mounts may be created as root. Restrict ownership changes to Contributarr's
# own data directory, then permanently drop privileges before Python starts.
mkdir -p /data/sessions
chown 10001:10001 /data /data/sessions
for file in /data/contributarr.db /data/contributarr.db-shm /data/contributarr.db-wal; do
    if [ -e "$file" ]; then chown 10001:10001 "$file"; fi
done

exec gosu 10001:10001 "$@"
