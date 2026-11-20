#!/bin/sh
# The artifact user, its policy and the policy's attachment (README.md,
# Artifacts), made with `rc` and the root credentials. Every step passes when
# its result already exists, so a Job that follows a half-finished one ends
# well and nothing is removed by hand: the policy is written again (its file
# may have changed), the user is added only when it is missing, and the
# attachment is accepted when the user already carries the policy.
set -eu

POLICY=matchmark-artifacts
ENDPOINT=http://rustfs.matchmark.svc.cluster.local:9000

rc alias set local "$ENDPOINT" "$ROOT_ACCESS_KEY" "$ROOT_SECRET_KEY"

rc admin policy create local/ "$POLICY" /opt/artifacts/policy.json
echo "policy: ok"

if rc admin user info local/ "$ACCESS_KEY_ID" >/dev/null 2>&1; then
  echo "user: ok (already there)"
else
  rc admin user add local/ "$ACCESS_KEY_ID" "$SECRET_ACCESS_KEY"
  echo "user: ok (added)"
fi

if rc admin policy attach local/ "$POLICY" --user "$ACCESS_KEY_ID"; then
  echo "attachment: ok"
elif rc admin user info local/ "$ACCESS_KEY_ID" 2>&1 | grep -q "$POLICY"; then
  echo "attachment: ok (already there)"
else
  echo "attachment: FAILED"
  exit 1
fi
