#!/usr/bin/env bash
set -euo pipefail

TARGET_REPO_URL="${OMBRE_BOOTSTRAP_REPO_URL:-https://github.com/Yinglianchun/Ombre-Brain.git}"
TARGET_BRANCH="${OMBRE_BRANCH:-main}"
REMOTE="${OMBRE_REMOTE:-origin}"

prompt_yes_no() {
  local question="$1"
  if [[ "${YES:-0}" == "1" ]]; then
    return 0
  fi
  if [[ ! -r /dev/tty ]]; then
    echo "${question} Set YES=1 to run non-interactively." >&2
    return 1
  fi
  local answer
  printf '%s [y/N] ' "${question}" >/dev/tty
  read -r answer </dev/tty || answer=""
  case "${answer}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

has_tracked_changes() {
  ! git diff --quiet || ! git diff --cached --quiet
}

ensure_clean_tracked_files() {
  if has_tracked_changes; then
    echo "Tracked files have local changes; bootstrap update stopped." >&2
    echo "Untracked data such as .env, config.yaml, buckets/ and state/ is fine," >&2
    echo "but tracked code changes must be committed or stashed first." >&2
    echo "" >&2
    git status --short --untracked-files=no >&2 || true
    return 1
  fi
}

backup_current_head() {
  local target_branch="$1"
  local stamp safe_branch backup_branch suffix
  stamp="$(date +%Y%m%d-%H%M%S)"
  safe_branch="${target_branch//\//-}"
  backup_branch="archive/bootstrap-before-${safe_branch}-${stamp}"
  suffix=0
  while git show-ref --verify --quiet "refs/heads/${backup_branch}"; do
    suffix=$((suffix + 1))
    backup_branch="archive/bootstrap-before-${safe_branch}-${stamp}-${suffix}"
  done
  git branch "${backup_branch}" HEAD
  printf '%s\n' "${backup_branch}"
}

if ! command -v git >/dev/null 2>&1; then
  echo "git not found. Install git first." >&2
  exit 1
fi

if ! repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "Run this inside an existing Ombre-Brain deployment checkout." >&2
  echo "For a fresh install, clone ${TARGET_REPO_URL} first." >&2
  exit 1
fi

cd "${repo_root}"

if [[ ! -f README.md || ! -d scripts ]]; then
  echo "This directory does not look like an Ombre-Brain checkout: ${repo_root}" >&2
  prompt_yes_no "Continue anyway?" || exit 1
fi

echo "Repo: ${repo_root}"
echo "Target: ${TARGET_REPO_URL} (${TARGET_BRANCH})"
echo "This updates only the git remote and code checkout."
echo "It does not delete untracked runtime data such as .env, config.yaml, buckets/ or state/."

ensure_clean_tracked_files

current_url="$(git remote get-url "${REMOTE}" 2>/dev/null || true)"
if [[ -n "${current_url}" && "${current_url}" != "${TARGET_REPO_URL}" ]]; then
  echo "Current ${REMOTE}: ${current_url}"
  echo "New ${REMOTE}:     ${TARGET_REPO_URL}"
  prompt_yes_no "Change ${REMOTE} to the new fork repo?" || exit 1
  git remote set-url "${REMOTE}" "${TARGET_REPO_URL}"
elif [[ -z "${current_url}" ]]; then
  prompt_yes_no "Add ${REMOTE} -> ${TARGET_REPO_URL}?" || exit 1
  git remote add "${REMOTE}" "${TARGET_REPO_URL}"
else
  echo "${REMOTE} already points to ${TARGET_REPO_URL}"
fi

echo "Fetch ${REMOTE}/${TARGET_BRANCH}..."
git fetch "${REMOTE}" "${TARGET_BRANCH}"

target_sha="$(git rev-parse FETCH_HEAD)"
head_sha="$(git rev-parse HEAD)"
current_branch="$(git branch --show-current 2>/dev/null || true)"
backup_branch=""

if [[ "${head_sha}" != "${target_sha}" ]]; then
  backup_branch="$(backup_current_head "${TARGET_BRANCH}")"
  echo "Saved current code as ${backup_branch}"
fi

if [[ "${current_branch}" != "${TARGET_BRANCH}" ]]; then
  if git show-ref --verify --quiet "refs/heads/${TARGET_BRANCH}"; then
    git switch "${TARGET_BRANCH}"
  else
    git switch -c "${TARGET_BRANCH}" FETCH_HEAD
  fi
fi

git reset --hard FETCH_HEAD
git branch --set-upstream-to="${REMOTE}/${TARGET_BRANCH}" "${TARGET_BRANCH}" >/dev/null 2>&1 || true

echo "Bootstrap update done."
echo "HEAD: $(git rev-parse --short HEAD)"
if [[ -n "${backup_branch}" ]]; then
  echo "Old code branch: ${backup_branch}"
fi
echo "Next: bash scripts/one_click.sh"
