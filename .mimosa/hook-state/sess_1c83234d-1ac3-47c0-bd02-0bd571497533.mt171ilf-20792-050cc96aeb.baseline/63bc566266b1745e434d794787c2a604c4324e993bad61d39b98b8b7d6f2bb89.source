#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_ops_common.sh"

cd "$(ombre_repo_root)"

COMPOSE_FILE="$(ombre_compose_file)"
OMBRE_SERVICE="${OMBRE_SERVICE:-ombre-brain}"
BATCH_SIZE="${BATCH_SIZE:-20}"

if [[ "${YES:-}" != "1" && "$*" != *"--dry-run"* ]]; then
  echo "This will refresh embeddings for every bucket and may use many embedding API calls."
  read -r -p "Continue? [y/N] " answer
  case "${answer}" in
    y|Y|yes|YES) ;;
    *) echo "Canceled."; exit 0 ;;
  esac
fi

echo "Rebuild all embeddings in ${OMBRE_SERVICE} (${COMPOSE_FILE})"
ombre_compose -f "${COMPOSE_FILE}" exec -T "${OMBRE_SERVICE}" \
  python backfill_embeddings.py --refresh-all --batch-size "${BATCH_SIZE}" "$@"
