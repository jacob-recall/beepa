#!/bin/sh
# Build the isolated browser push helper. Existing VAPID keys are preserved.
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PUSH_CONTEXT=$(mktemp -d)
trap 'rm -rf "$PUSH_CONTEXT"' EXIT HUP INT TERM
cp -f "$TASK_ROOT/master/notification-push/Dockerfile" "$PUSH_CONTEXT/Dockerfile"
cp -f "$TASK_ROOT/master/notifications.py" "$PUSH_CONTEXT/notifications.py"
cp -f "$TASK_ROOT/requirements-notifications.txt" "$PUSH_CONTEXT/requirements-notifications.txt"
docker build -t beepa-notification-push:local "$PUSH_CONTEXT"
