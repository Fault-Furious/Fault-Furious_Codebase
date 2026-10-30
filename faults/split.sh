#!/bin/sh
# Stockholm-Helsinki split: cut the only network the two app servers share.
#   faults/split.sh        cut
#   faults/split.sh reset  heal
set -e
cd "$(dirname "$0")/.."
net=${PEER_NETWORK:-nordic-peer}
container=$(docker compose ps -q stockholm)
if [ "$1" = reset ]; then
  docker network connect --alias stockholm "$net" "$container"
else
  docker network disconnect "$net" "$container"
fi
