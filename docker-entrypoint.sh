#!/bin/sh
# Indítás: a /data kötet tulajdonosa a futtató felhasználó legyen (a Coolify tartós tárolója gyökér-tulajdonú is lehet),
# majd jogosultság-csökkentés: a motor soha nem fut rendszergazdaként.
set -e
DATA="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA/cache"
    chown -R 10001:10001 "$DATA" 2>/dev/null || true
    exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
fi
exec "$@"
