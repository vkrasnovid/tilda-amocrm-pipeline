#!/bin/sh
set -e

# Ensure /data directory exists and is owned by appuser.
# This handles Docker named volumes that are initialised with root ownership.
mkdir -p /data
chown -R appuser:appuser /data

# Drop privileges and exec the service command as appuser.
exec gosu appuser "$@"
