#!/usr/bin/env bash
# yaml_paths.sh - switch device YAML paths between local checkout and remote release refs.
#
# Convention
# Each YAML carries ONE active value per resource: ext_components_source,
# voip_stack_components_source, audio_stack_components_source,
# runtime_controller_components_source, assets_base, and each `packages:` entry.
# No dual-mode commented shadow
# lines. This script is the single source of truth for those values:
# rewriting in place from local→remote (or vice versa) is mechanical, no
# manual edit needed.
#
# Workflow:
# - Local development: working tree in local mode. `esphome compile` picks up sibling
#   checkouts directly.
# - Remote main: `./yaml_paths.sh --remote main`.
# - Remote development: `./yaml_paths.sh --remote dev`.
# - Immutable release refs: `./yaml_paths.sh --remote tag --tag TAG`, or set
#   each repository independently when their release versions differ.
# - Advanced/custom remote refs remain available through `remote`.
#
# How it works
# Rewrite targets per YAML:
#   1. ext_components_source              → string substitution (single line)
#   2. voip_stack_components_source      → string substitution (single line,
#                                           optional; only YAMLs using the
#                                           split ESP VoIP stack repo have it)
#   3. audio_stack_components_source      → string substitution (single line,
#                                           optional; only YAMLs using the
#                                           split ESP audio stack repo have it)
#   4. runtime_controller_components_source
#                                         → string substitution (single line,
#                                           optional; only full YAMLs using the
#                                           split runtime controller repo have it)
#   5. assets_base                        → string substitution (single line,
#                                           optional;
#                                some yamls don't use external assets)
#   6. packages: <key>: VALUE             → one line per package entry (N entries)
#
# For (1)+(2)+(3)+(4): straight `sed` of the active value.
# For (5): per-line awk replacement, because the path conversion needs
# `realpath --relative-to` per yaml (each yaml lives at a different depth).
#
# ESPHome conventions we rely on
# - `external_components: source: github://OWNER/REPO@BRANCH` defaults to
#   looking up components in the repo's `esphome/components/` subfolder.
#   So local equivalent must be `<reldepth>/esphome/components` (full path
#   to that subfolder), not just the repo root.
# - `packages: <key>: github://OWNER/REPO/<inner_path>@BRANCH` resolves
#   to the file at `<inner_path>` inside the repo at that branch. Local
#   equivalent: `!include <relpath_to_that_file>`.
# - `assets_base` is just a string substitution prefix used by the YAML's
#   own `image:`/`font:` blocks. Local: `<reldepth>/`. Remote: full HTTPS
#   raw URL `https://github.com/OWNER/REPO/raw/BRANCH/`.
#
# Edge cases handled
# - Package keys with digits (`s3_base`, `status_led`): regex uses
#   `[a-zA-Z_][a-zA-Z0-9_]*` (identifier shape, not just letters).
# - Yaml depth varies (3 or 4 levels deep under `yamls/`): all paths
#   computed dynamically via `realpath --relative-to`. No hardcoded depth.
# - Yamls without `assets_base` (e.g. minimal voip-only): the sed
#   substitution is a no-op when the line is absent.
# - Roundtrip local→remote→local is byte-identical (verified via md5sum).

set -euo pipefail

# Defaults
DEFAULT_URL="github://n-IA-hane/esphome-intercom"
DEFAULT_INTERCOM_BRANCH="main"
DEFAULT_VOIP_STACK_URL="github://n-IA-hane/esphome-voip-stack"
DEFAULT_VOIP_STACK_BRANCH="main"
VOIP_STACK_ROOT_DEFAULT="../esphome-voip-stack"
DEFAULT_AUDIO_STACK_URL="github://n-IA-hane/esphome-audio-stack"
DEFAULT_AUDIO_STACK_BRANCH="main"
AUDIO_STACK_ROOT_DEFAULT="../esphome-audio-stack"
DEFAULT_RUNTIME_CONTROLLER_URL="github://n-IA-hane/esphome-runtime-controller"
DEFAULT_RUNTIME_CONTROLLER_BRANCH="main"
RUNTIME_CONTROLLER_ROOT_DEFAULT="../esphome-runtime-controller"
ASSETS_HOST="https://github.com"   # for assets_base remote URL composition

# Helpers
err()  { echo "error: $*" >&2; exit 1; }
log()  { echo "$*" >&2; }
note() { echo "  $*" >&2; }

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || err "not in a git repo"
}

current_branch() {
  git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main"
}

# Strip "github://OWNER/REPO" → "OWNER/REPO"
url_owner_repo() {
  echo "$1" | sed -E 's|^github://([^/]+/[^/@]+).*|\1|'
}

normalize_ref() {
  case "$1" in
    local|LOCAL) echo "local" ;;
    *) echo "$1" ;;
  esac
}

# Find production YAMLs that should be toggle-managed.
# Excludes: ESPHome build cache, secrets file (per-device WiFi credentials,
# never published), `yamls/debug` (local diagnostics, not downloadable release
# presets), and `*_NOT_READY.yaml` (work-in-progress yamls staged but not yet
# wired up to the toggle pattern).
find_yamls() {
  local root="$1"
  find "$root/yamls" -type f -name '*.yaml' \
    -not -path '*/.esphome/*' \
    -not -path "$root/yamls/debug/*" \
    -not -name 'secrets.yaml' \
    -not -name '*_NOT_READY.yaml' \
    | sort
}

# Classify a YAML: "local" if all toggle sites use relative paths/!include,
# "remote" if all use github://, "mixed" if both forms appear (= manual edit
# left it inconsistent, lint should fail), "unknown" if no toggle site found
# (yaml doesn't actually use the toggle pattern, e.g. a fragment).
detect_mode() {
  local f="$1" has_local=0 has_remote=0
  # Remote markers: ext_components_source/packages pointing at this repo.
  # Third-party github:// packages are allowed in local mode and must not make
  # the YAML look mixed.
  if grep -qF "ext_components_source: \"${DEFAULT_URL}" "$f" \
     || grep -qF "voip_stack_components_source: \"${DEFAULT_VOIP_STACK_URL}" "$f" \
     || grep -qF "audio_stack_components_source: \"${DEFAULT_AUDIO_STACK_URL}" "$f" \
     || grep -qF "runtime_controller_components_source: \"${DEFAULT_RUNTIME_CONTROLLER_URL}" "$f" \
     || grep -qF ": ${DEFAULT_RUNTIME_CONTROLLER_URL}/" "$f" \
     || grep -qF ": ${DEFAULT_URL}/" "$f"; then
    has_remote=1
  fi
  # Local markers: ext_components_source with relative path (`"../`), OR any
  # packages: entry using !include yaml tag.
  if grep -qE '^[[:space:]]*ext_components_source:[[:space:]]*"\.\./' "$f" \
     || grep -qE '^[[:space:]]*audio_stack_components_source:[[:space:]]*"\.\./' "$f" \
     || grep -qE '^[[:space:]]*voip_stack_components_source:[[:space:]]*"\.\./' "$f" \
     || grep -qE '^[[:space:]]*runtime_controller_components_source:[[:space:]]*"\.\./' "$f" \
     || grep -qE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*!include' "$f"; then
    has_local=1
  fi
  if [[ $has_local -eq 1 && $has_remote -eq 1 ]]; then echo "mixed"
  elif [[ $has_remote -eq 1 ]]; then echo "remote"
  elif [[ $has_local -eq 1 ]]; then echo "local"
  elif grep -qE '^esphome:[[:space:]]*$' "$f"; then echo "unknown"
  else echo "fragment"; fi
}

# Rewrite a single YAML to LOCAL mode (relative paths).
# `relroot` is the relative path from the yaml's directory to the repo root,
# computed dynamically so depth differences across yamls (3 vs 4 levels) are
# handled automatically. With trailing slash unless yaml IS at repo root
# (degenerate case, none of our yamls live there).
to_local() {
  local f="$1" root="$2" voip_root="$3" audio_root="$4" runtime_root="$5" yaml_dir relroot voip_relroot audio_relroot runtime_relroot
  yaml_dir=$(dirname "$f")
  relroot=$(realpath --relative-to="$yaml_dir" "$root")
  [[ "$relroot" == "." ]] && relroot="" || relroot="$relroot/"
  voip_relroot=$(realpath --relative-to="$yaml_dir" "$voip_root")
  [[ "$voip_relroot" == "." ]] && voip_relroot="" || voip_relroot="$voip_relroot/"
  audio_relroot=$(realpath --relative-to="$yaml_dir" "$audio_root")
  [[ "$audio_relroot" == "." ]] && audio_relroot="" || audio_relroot="$audio_relroot/"
  runtime_relroot=$(realpath --relative-to="$yaml_dir" "$runtime_root")
  [[ "$runtime_relroot" == "." ]] && runtime_relroot="" || runtime_relroot="$runtime_relroot/"

  # 1) ext_components_source → "<relroot>esphome/components"
  #    e.g. yaml at depth 3 ⇒ relroot="../../../" ⇒ value "../../../esphome/components"
  sed -i -E "s|^([[:space:]]*ext_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${relroot}esphome/components\"|" "$f"

  # 1b) voip_stack_components_source → sibling esp-voip-stack repo
  sed -i -E "s|^([[:space:]]*voip_stack_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${voip_relroot}esphome/components\"|" "$f"

  # 1c) audio_stack_components_source → sibling esp-audio-stack repo
  sed -i -E "s|^([[:space:]]*audio_stack_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${audio_relroot}esphome/components\"|" "$f"

  # 1d) runtime_controller_components_source → sibling esp-runtime-controller repo
  sed -i -E "s|^([[:space:]]*runtime_controller_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${runtime_relroot}esphome/components\"|" "$f"

  # 2) assets_base → "<relroot>"
  #    Bare relroot (e.g. "../../../") points to the repo root, where assets/
  #    and similar siblings live. If yaml IS at repo root, fall back to "./".
  local assets_value="${relroot}"
  [[ -z "$assets_value" ]] && assets_value="./"
  sed -i -E "s|^([[:space:]]*assets_base:)[[:space:]]*\"[^\"]*\"|\1 \"${assets_value}\"|" "$f"

  # 3) packages: each "<key>: github://OWNER/REPO/INNER@BRANCH" → "<key>: !include <relpath>"
  # Per-line because the relative path back to each package file depends on
  # the yaml's location. Steps:
  #   a. grep all package lines with their line numbers
  #   b. for each line, parse out indent, key, INNER (path inside repo),
  #      branch (discarded, going local).
  #   c. compute target_abs (where INNER lives in the working tree) and
  #      target_rel (path from this yaml's directory to that target).
  #   d. replace the line with "<indent><key>: !include <target_rel>".
  # awk used instead of sed for the final replacement: the new line contains
  # path separators that would need escaping in sed substitutions.
  local lines
  lines=$(grep -nE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*github://' "$f" || true)
  if [[ -n "$lines" ]]; then
    while IFS= read -r line_info; do
      local lineno orig key inner target_abs target_rel indent new
      lineno=$(echo "$line_info" | cut -d: -f1)
      orig=$(sed -n "${lineno}p" "$f")
      key=$(echo "$orig" | sed -E 's|^[[:space:]]*([a-zA-Z_][a-zA-Z0-9_]*):.*|\1|')
      indent=$(echo "$orig" | sed -E 's|^( *).*|\1|')
      # INNER = path between repo + the @branch suffix:
      #   "github://owner/repo/packages/foo.yaml@branch" → "packages/foo.yaml"
      local value
      value=$(echo "$orig" | sed -E 's|^[[:space:]]*[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*([^[:space:]]+).*$|\1|')
      if [[ "$value" == "$DEFAULT_URL/"*@* ]]; then
        inner="${value#"$DEFAULT_URL/"}"
        inner="${inner%@*}"
        target_abs="$root/$inner"
      elif [[ "$value" == "$DEFAULT_RUNTIME_CONTROLLER_URL/"*@* ]]; then
        inner="${value#"$DEFAULT_RUNTIME_CONTROLLER_URL/"}"
        inner="${inner%@*}"
        target_abs="$runtime_root/$inner"
      else
        continue
      fi
      target_rel=$(realpath --relative-to="$yaml_dir" "$target_abs")
      new="${indent}${key}: !include ${target_rel}"
      awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    done <<< "$lines"
  fi
}

# Rewrite a single YAML to REMOTE mode (github://owner/repo paths + branch).
# Symmetric to to_local but inverse direction. Reads the local relpath of
# each package, resolves it to an absolute path, then computes the path
# relative to the repo root (= the INNER part of the github:// URL).
to_remote() {
  local f="$1" root="$2" url="$3" branch="$4" voip_url="$5" voip_branch="$6" audio_url="$7" audio_branch="$8" runtime_url="$9" runtime_branch="${10}" runtime_root="${11}"
  local yaml_dir owner_repo assets_url
  yaml_dir=$(dirname "$f")
  owner_repo=$(url_owner_repo "$url")
  # assets_base remote uses HTTPS raw URLs (not the github:// shorthand)
  # because it's just a string concatenated with image/font paths in YAML,
  # not consumed by ESPHome's github:// resolver.
  assets_url="${ASSETS_HOST}/${owner_repo}/raw/${branch}/"

  # 1) ext_components_source → "github://OWNER/REPO@BRANCH"
  #    No subfolder needed: ESPHome defaults to <repo>/esphome/components/.
  sed -i -E "s|^([[:space:]]*ext_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${url}@${branch}\"|" "$f"

  # 1b) voip_stack_components_source → split ESP VoIP stack repo.
  sed -i -E "s|^([[:space:]]*voip_stack_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${voip_url}@${voip_branch}\"|" "$f"

  # 1c) audio_stack_components_source → split ESP audio stack repo.
  # This branch is intentionally independent from the intercom branch:
  # release/dev intercom YAMLs can consume the stable audio-stack main branch.
  sed -i -E "s|^([[:space:]]*audio_stack_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${audio_url}@${audio_branch}\"|" "$f"

  # 1d) runtime_controller_components_source → split runtime controller repo.
  sed -i -E "s|^([[:space:]]*runtime_controller_components_source:)[[:space:]]*\"[^\"]*\"|\1 \"${runtime_url}@${runtime_branch}\"|" "$f"

  # 2) assets_base → "https://github.com/OWNER/REPO/raw/BRANCH/"
  sed -i -E "s|^([[:space:]]*assets_base:)[[:space:]]*\"[^\"]*\"|\1 \"${assets_url}\"|" "$f"

  # 3) packages.
  # If a YAML is already remote, retarget existing github:// package entries
  # that point to this repo first. This is the release case: dev -> main
  # should not require a local-mode roundtrip just to change the branch suffix.
  #
  # Keep third-party github:// packages untouched. Some upstream release YAMLs
  # deliberately live on a non-main branch, and rewriting those would create a
  # broken preset even though our own packages/components are correctly remote.
  local remote_lines
  remote_lines=$(grep -nE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*github://' "$f" || true)
  if [[ -n "$remote_lines" ]]; then
    while IFS= read -r line_info; do
      local lineno orig prefix value inner new
      lineno=$(echo "$line_info" | cut -d: -f1)
      orig=$(sed -n "${lineno}p" "$f")
      prefix=$(echo "$orig" | sed -E 's|^([[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*).*|\1|')
      value=$(echo "$orig" | sed -E 's|^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*([^[:space:]]+).*$|\1|')
      if [[ "$value" == "$url/"*@* ]]; then
        inner="${value#"$url/"}"
        inner="${inner%@*}"
        new="${prefix}${url}/${inner}@${branch}"
        awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
      elif [[ "$value" == "$runtime_url/"*@* ]]; then
        inner="${value#"$runtime_url/"}"
        inner="${inner%@*}"
        new="${prefix}${runtime_url}/${inner}@${runtime_branch}"
        awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
      fi
    done <<< "$remote_lines"
  fi

  # Then convert local includes:
  # each "<key>: !include <relpath>" -> "<key>: github://OWNER/REPO/INNER@BRANCH"
  # Steps mirror to_local in reverse:
  #   a. parse indent, key, relpath from the !include line.
  #   b. realpath -m to absolute (with -m so it works even if a future
  #      reorganisation has moved a file: doesn't error out on missing).
  #   c. realpath --relative-to repo_root → INNER (path inside the repo).
  #   d. write "<indent><key>: <url>/<inner>@<branch>".
  local lines
  lines=$(grep -nE '^[[:space:]]+[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*!include[[:space:]]' "$f" || true)
  if [[ -n "$lines" ]]; then
    while IFS= read -r line_info; do
      local lineno orig key relpath target_abs inner indent new
      lineno=$(echo "$line_info" | cut -d: -f1)
      orig=$(sed -n "${lineno}p" "$f")
      key=$(echo "$orig" | sed -E 's|^[[:space:]]*([a-zA-Z_][a-zA-Z0-9_]*):.*|\1|')
      indent=$(echo "$orig" | sed -E 's|^( *).*|\1|')
      relpath=$(echo "$orig" | sed -E 's|^[[:space:]]*[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*!include[[:space:]]+(.+)$|\1|')
      target_abs=$(realpath -m "$yaml_dir/$relpath")
      if [[ "$target_abs" == "$runtime_root"/packages/runtime_controller/* ]]; then
        inner=$(realpath --relative-to="$runtime_root" "$target_abs")
        new="${indent}${key}: ${runtime_url}/${inner}@${runtime_branch}"
      else
        inner=$(realpath --relative-to="$root" "$target_abs")
        new="${indent}${key}: ${url}/${inner}@${branch}"
      fi
      awk -v ln="$lineno" -v repl="$new" 'NR==ln{print repl; next}{print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    done <<< "$lines"
  fi
}

# Commands
cmd_status() {
  local root branch
  root=$(repo_root)
  branch=$(current_branch)
  log "Repo:           $root"
  log "Current branch: $branch"
  log ""
  printf "%-65s  %s\n" "YAML" "MODE"
  printf "%-65s  %s\n" "-----------------------------------------------------------------" "-------"
  while IFS= read -r f; do
    local rel mode
    rel=$(realpath --relative-to="$root" "$f")
    [[ -n "${ONLY_FILE:-}" && "$rel" != "$ONLY_FILE" && "$f" != "$ONLY_FILE" ]] && continue
    mode=$(detect_mode "$f")
    printf "%-65s  %s\n" "$rel" "$mode"
  done < <(find_yamls "$root")
}

cmd_local() {
  local root voip_root audio_root runtime_root
  root=$(repo_root)
  voip_root="${VOIP_STACK_ROOT_ARG:-$root/$VOIP_STACK_ROOT_DEFAULT}"
  voip_root=$(realpath -m "$voip_root")
  audio_root="${AUDIO_STACK_ROOT_ARG:-$root/$AUDIO_STACK_ROOT_DEFAULT}"
  audio_root=$(realpath -m "$audio_root")
  runtime_root="${RUNTIME_CONTROLLER_ROOT_ARG:-$root/$RUNTIME_CONTROLLER_ROOT_DEFAULT}"
  runtime_root=$(realpath -m "$runtime_root")
  log "Switching to LOCAL mode (relative paths)"
  log "  VoIP stack root:  $voip_root"
  log "  Audio stack root: $audio_root"
  log "  Runtime controller root: $runtime_root"
  log ""
  while IFS= read -r f; do
    local rel
    rel=$(realpath --relative-to="$root" "$f")
    [[ -n "${ONLY_FILE:-}" && "$rel" != "$ONLY_FILE" && "$f" != "$ONLY_FILE" ]] && continue
    to_local "$f" "$root" "$voip_root" "$audio_root" "$runtime_root"
    # Local runtime-controller packages live in the sibling runtime repo, not in
    # the intercom repo. Keep the full-profile package include colocated with
    # the component source so local testing exercises the split repository.
    yaml_dir=$(dirname "$f")
    runtime_relroot=$(realpath --relative-to="$yaml_dir" "$runtime_root")
    [[ "$runtime_relroot" == "." ]] && runtime_relroot="" || runtime_relroot="$runtime_relroot/"
    if grep -qE '^[[:space:]]+runtime_controller:[[:space:]]*!include[[:space:]]+' "$f"; then
      sed -i -E "s|^([[:space:]]+runtime_controller:)[[:space:]]*!include[[:space:]]+.*packages/runtime_controller/(full_controller(_no_led)?\\.yaml)|\\1 !include ${runtime_relroot}packages/runtime_controller/\\2|" "$f"
    fi
    note "$rel"
  done < <(find_yamls "$root")
}

cmd_remote() {
  local root url branch voip_url voip_branch audio_url audio_branch runtime_url runtime_branch runtime_root
  root=$(repo_root)
  url="${URL_ARG:-$DEFAULT_URL}"
  branch="${INTERCOM_REF_ARG:-${BRANCH_ARG:-$DEFAULT_INTERCOM_BRANCH}}"
  voip_url="${VOIP_STACK_URL_ARG:-$DEFAULT_VOIP_STACK_URL}"
  voip_branch="${VOIP_STACK_REF_ARG:-${VOIP_STACK_BRANCH_ARG:-$DEFAULT_VOIP_STACK_BRANCH}}"
  audio_url="${AUDIO_STACK_URL_ARG:-$DEFAULT_AUDIO_STACK_URL}"
  audio_branch="${AUDIO_STACK_REF_ARG:-${AUDIO_STACK_BRANCH_ARG:-$DEFAULT_AUDIO_STACK_BRANCH}}"
  runtime_url="${RUNTIME_CONTROLLER_URL_ARG:-$DEFAULT_RUNTIME_CONTROLLER_URL}"
  runtime_branch="${RUNTIME_CONTROLLER_REF_ARG:-${RUNTIME_CONTROLLER_BRANCH_ARG:-$DEFAULT_RUNTIME_CONTROLLER_BRANCH}}"
  branch=$(normalize_ref "$branch")
  voip_branch=$(normalize_ref "$voip_branch")
  audio_branch=$(normalize_ref "$audio_branch")
  runtime_branch=$(normalize_ref "$runtime_branch")
  runtime_root="${RUNTIME_CONTROLLER_ROOT_ARG:-$root/$RUNTIME_CONTROLLER_ROOT_DEFAULT}"
  runtime_root=$(realpath -m "$runtime_root")
  if [[ "$branch" == "local" || "$voip_branch" == "local" || "$audio_branch" == "local" || "$runtime_branch" == "local" ]]; then
    err "remote currently accepts branch/tag refs only; use 'local' command for all-local mode"
  fi
  log "Switching to REMOTE mode"
  log "  URL:                $url"
  log "  Intercom ref:       $branch"
  log "  VoIP stack URL:     $voip_url"
  log "  VoIP stack ref:     $voip_branch"
  log "  Audio stack URL:    $audio_url"
  log "  Audio stack ref:    $audio_branch"
  log "  Runtime URL:        $runtime_url"
  log "  Runtime ref:        $runtime_branch"
  log ""
  while IFS= read -r f; do
    local rel
    rel=$(realpath --relative-to="$root" "$f")
    [[ -n "${ONLY_FILE:-}" && "$rel" != "$ONLY_FILE" && "$f" != "$ONLY_FILE" ]] && continue
    to_remote "$f" "$root" "$url" "$branch" "$voip_url" "$voip_branch" "$audio_url" "$audio_branch" "$runtime_url" "$runtime_branch" "$runtime_root"
    note "$rel"
  done < <(find_yamls "$root")
}

cmd_remote_profile() {
  local profile="$1"
  case "$profile" in
    main|dev)
      INTERCOM_REF_ARG="${INTERCOM_REF_ARG:-$profile}"
      VOIP_STACK_REF_ARG="${VOIP_STACK_REF_ARG:-$profile}"
      AUDIO_STACK_REF_ARG="${AUDIO_STACK_REF_ARG:-$profile}"
      RUNTIME_CONTROLLER_REF_ARG="${RUNTIME_CONTROLLER_REF_ARG:-$profile}"
      ;;
    tag)
      if [[ -n "$GLOBAL_TAG_ARG" ]]; then
        INTERCOM_REF_ARG="${INTERCOM_REF_ARG:-$GLOBAL_TAG_ARG}"
        VOIP_STACK_REF_ARG="${VOIP_STACK_REF_ARG:-$GLOBAL_TAG_ARG}"
        AUDIO_STACK_REF_ARG="${AUDIO_STACK_REF_ARG:-$GLOBAL_TAG_ARG}"
        RUNTIME_CONTROLLER_REF_ARG="${RUNTIME_CONTROLLER_REF_ARG:-$GLOBAL_TAG_ARG}"
      fi
      [[ -n "$INTERCOM_REF_ARG" ]] \
        || err "tag mode requires --tag REF or --intercom REF"
      [[ -n "$VOIP_STACK_REF_ARG" ]] \
        || err "tag mode requires --tag REF or --voip REF"
      [[ -n "$AUDIO_STACK_REF_ARG" ]] \
        || err "tag mode requires --tag REF or --audio REF"
      [[ -n "$RUNTIME_CONTROLLER_REF_ARG" ]] \
        || err "tag mode requires --tag REF or --runtime REF"
      ;;
    *) err "unknown remote profile: $profile" ;;
  esac
  cmd_remote
}

cmd_check() {
  local root rc=0
  root=$(repo_root)
  while IFS= read -r f; do
    local rel mode
    rel=$(realpath --relative-to="$root" "$f")
    mode=$(detect_mode "$f")
    if [[ "$mode" == "mixed" ]]; then
      log "FAIL: $rel ($mode)"
      rc=1
    elif [[ "$mode" == "unknown" ]]; then
      log "FAIL: $rel (standalone YAML is not path-managed)"
      rc=1
    elif [[ -n "$EXPECT_MODE_ARG" && "$mode" != "fragment" \
      && "$mode" != "$EXPECT_MODE_ARG" ]]; then
      log "FAIL: $rel ($mode, expected $EXPECT_MODE_ARG)"
      rc=1
    fi
    if grep -qE '^[[:space:]]*-[[:space:]]*!include[[:space:]]+' "$f"; then
      log "FAIL: $rel (nested list !include is not portable outside the repo)"
      rc=1
    fi
  done < <(find_yamls "$root")
  if [[ $rc -eq 0 ]]; then log "OK: all YAMLs consistent."; fi
  exit $rc
}

usage() {
  cat <<EOF
yaml_paths.sh - switch device YAML paths between local checkout and remote release refs.

Usage:
  $(basename "$0") --local [options]
  $(basename "$0") --remote main [options]
  $(basename "$0") --remote dev [options]
  $(basename "$0") --remote tag [tag options]
  $(basename "$0") <command> [options]

Commands:
  status                          Print mode (local/remote/mixed) per YAML
  --local                         Rewrite all YAMLs to LOCAL mode
  --remote main                   Use remote @main for every project repo
  --remote dev                    Use remote @dev for every project repo
  --remote tag                    Use immutable refs supplied through --tag,
                                  or one explicit ref for every project repo
  remote [options]                Rewrite all YAMLs to REMOTE mode
  check                           Lint path consistency

Options:
  --url URL       e.g. github://n-IA-hane/esphome-intercom (default)
  --branch B      legacy alias for --intercom B
  --tag REF       In --remote tag mode, use the same immutable ref for all repos
  --intercom REF  intercom/VoIP repo branch/tag (default: main)
  --voip-stack-url URL
                  e.g. github://n-IA-hane/esphome-voip-stack (default)
  --voip-stack-branch B
                  legacy alias for --voip B
  --voip REF      esphome-voip-stack branch/tag (default: main)
  --voip-stack-root PATH
                  local esphome-voip-stack checkout (default: ../esphome-voip-stack)
  --audio-stack-url URL
                  e.g. github://n-IA-hane/esphome-audio-stack (default)
  --audio-stack-branch B
                  legacy alias for --audio B
  --audio REF     esp-audio-stack branch/tag (default: main)
  --audio-stack-root PATH
                  local esp-audio-stack checkout (default: ../esphome-audio-stack)
  --runtime-controller-url URL
                  e.g. github://n-IA-hane/esphome-runtime-controller (default)
  --runtime-controller-branch B
                  legacy alias for --runtime B
  --runtime REF   esphome-runtime-controller branch/tag (default: main)
  --runtime-controller-root PATH
                  local esphome-runtime-controller checkout (default: ../esphome-runtime-controller)
  --expect MODE   With check, require local or remote mode for standalone YAMLs
  --file PATH     limit operation to a single YAML (relative to repo root or absolute)

Examples:
  $(basename "$0") status
  $(basename "$0") check --expect remote
  $(basename "$0") --local
  $(basename "$0") --remote main
  $(basename "$0") --remote dev
  $(basename "$0") --remote tag --tag TAG
  $(basename "$0") --remote tag --intercom INTERCOM_TAG --voip VOIP_TAG \
    --audio AUDIO_TAG --runtime RUNTIME_TAG
  $(basename "$0") remote --intercom main --voip main --audio main --runtime main
  $(basename "$0") remote --url github://my-fork/esphome-intercom --branch main
  $(basename "$0") remote --file yamls/voip-only/single-bus/generic-s3-voip.yaml --branch main
EOF
}

# Argument parsing
[[ $# -lt 1 ]] && { usage; exit 1; }

REMOTE_PROFILE=""
case "$1" in
  --local)
    cmd="local"
    shift
    ;;
  --remote)
    [[ $# -ge 2 ]] || err "--remote requires one of: main, dev, tag"
    cmd="remote-profile"
    REMOTE_PROFILE="$2"
    shift 2
    case "$REMOTE_PROFILE" in
      main|dev|tag) ;;
      *) err "--remote requires one of: main, dev, tag" ;;
    esac
    ;;
  *)
    cmd="$1"
    shift
    ;;
esac
URL_ARG=""
BRANCH_ARG=""
INTERCOM_REF_ARG=""
VOIP_STACK_URL_ARG=""
VOIP_STACK_BRANCH_ARG=""
VOIP_STACK_REF_ARG=""
VOIP_STACK_ROOT_ARG=""
AUDIO_STACK_URL_ARG=""
AUDIO_STACK_BRANCH_ARG=""
AUDIO_STACK_REF_ARG=""
AUDIO_STACK_ROOT_ARG=""
RUNTIME_CONTROLLER_URL_ARG=""
RUNTIME_CONTROLLER_BRANCH_ARG=""
RUNTIME_CONTROLLER_REF_ARG=""
RUNTIME_CONTROLLER_ROOT_ARG=""
ONLY_FILE=""
GLOBAL_TAG_ARG=""
EXPECT_MODE_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)    URL_ARG="$2"; shift 2 ;;
    --branch) BRANCH_ARG="$2"; shift 2 ;;
    --tag) GLOBAL_TAG_ARG="$2"; shift 2 ;;
    --intercom) INTERCOM_REF_ARG="$2"; shift 2 ;;
    --voip-stack-url) VOIP_STACK_URL_ARG="$2"; shift 2 ;;
    --voip-stack-branch) VOIP_STACK_BRANCH_ARG="$2"; shift 2 ;;
    --voip|--esp-voip-stack|--voip-stack) VOIP_STACK_REF_ARG="$2"; shift 2 ;;
    --voip-stack-root) VOIP_STACK_ROOT_ARG="$2"; shift 2 ;;
    --audio-stack-url) AUDIO_STACK_URL_ARG="$2"; shift 2 ;;
    --audio-stack-branch) AUDIO_STACK_BRANCH_ARG="$2"; shift 2 ;;
    --audio|--esp-audio-stack) AUDIO_STACK_REF_ARG="$2"; shift 2 ;;
    --audio-stack-root) AUDIO_STACK_ROOT_ARG="$2"; shift 2 ;;
    --runtime-controller-url) RUNTIME_CONTROLLER_URL_ARG="$2"; shift 2 ;;
    --runtime-controller-branch) RUNTIME_CONTROLLER_BRANCH_ARG="$2"; shift 2 ;;
    --runtime|--fsm|--runtime-controller) RUNTIME_CONTROLLER_REF_ARG="$2"; shift 2 ;;
    --runtime-controller-root) RUNTIME_CONTROLLER_ROOT_ARG="$2"; shift 2 ;;
    --expect)
      EXPECT_MODE_ARG="$2"
      [[ "$EXPECT_MODE_ARG" == "local" || "$EXPECT_MODE_ARG" == "remote" ]] \
        || err "--expect requires local or remote"
      shift 2
      ;;
    --file)   ONLY_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown option: $1 (run --help)" ;;
  esac
done

if [[ -n "$EXPECT_MODE_ARG" && "$cmd" != "check" ]]; then
  err "--expect is only valid with check"
fi

case "$cmd" in
  status) cmd_status ;;
  local)  cmd_local ;;
  remote-profile) cmd_remote_profile "$REMOTE_PROFILE" ;;
  remote) cmd_remote ;;
  check)  cmd_check ;;
  -h|--help) usage ;;
  *) err "unknown command: $cmd (run --help)" ;;
esac
