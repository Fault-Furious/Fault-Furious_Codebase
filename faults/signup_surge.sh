#!/bin/sh
# Signup surge: low max_connections on the user DB + a flood of POST /signup.
#   faults/signup_surge.sh [max_connections=10] [signup_rps=150]
#   faults/signup_surge.sh reset
set -e
cd "$(dirname "$0")/.."
if [ "$1" = reset ]; then
  docker compose up -d userdb && docker compose up -d --no-deps loadgen
else
  USERDB_MAX_CONNECTIONS=${1:-10} docker compose up -d userdb
  SIGNUP_RPS=${2:-150} docker compose up -d --no-deps loadgen
fi
