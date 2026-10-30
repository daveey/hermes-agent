#!/usr/bin/env bash
# Usage: store-github-app.sh <manifest-conversion-code>
# Exchanges the one-time manifest code (from the redirect after David clicks "Create GitHub App")
# for the App id / private key / webhook secret and stores them straight into SSM parameters.
# Nothing is printed or written to disk.
set -euo pipefail
CODE=$1
RESP=$(curl -fsS -X POST -H 'Accept: application/vnd.github+json' "https://api.github.com/app-manifests/$CODE/conversions")
put() { aws ssm put-parameter --region us-west-2 --overwrite --type "$3" --name "$1" --value "$2" >/dev/null; }
put /hermes-ci/github-app/id "$(jq -r .id <<<"$RESP")" String
put /hermes-ci/github-app/key-base64 "$(jq -r .pem <<<"$RESP" | base64 | tr -d "\n")" SecureString
put /hermes-ci/github-app/webhook-secret "$(jq -r .webhook_secret <<<"$RESP")" SecureString
echo "stored app id $(jq -r .id <<<"$RESP") ($(jq -r .slug <<<"$RESP"))"
