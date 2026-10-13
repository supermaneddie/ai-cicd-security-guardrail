CODE_QC_URL
#!/usr/bin/env bash

set -euo pipefail

: "${CODE_QC_URL:?CODE_QC_URL must be set}"
: "${CODE_QC_WS_URL:?CODE_QC_WS_URL must be set}"
TYPE="${TYPE:-all}"
FILES="${FILES:-changed_only}"
DATE="${DATE:-$(date '+%Y-%m-%d')}"
PUSH_COMMENT="${PUSH_COMMENT:-SRAA fixes by Codex on $DATE}"
PUSH_BRANCH="${PUSH_BRANCH:-${CI_COMMIT_BRANCH:-${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME:-}}}"
PUSH_USERNAME="${PUSH_USERNAME:-AI CICD Security Guardrail Bot}"
PUSH_EMAIL="${PUSH_EMAIL:-codeqc@gmail.com}"
CODEX_AUTH="${CODEX_AUTH:-.codex_auth}"
PROMPT="${PROMPT:-}"
PROMPT_FILE="${PROMPT_FILE:-.codeqc_prompt}"
SELECTOR_FILE="${SELECTOR_FILE:-.codeqc_selector}"
CODEX_ACTION="${CODEX_ACTION:-auto}"
RESULTS_ID="${RESULTS_ID:-}"

if [[ -z "$PUSH_BRANCH" ]]; then
  echo "PUSH_BRANCH, CI_COMMIT_BRANCH, or CI_MERGE_REQUEST_SOURCE_BRANCH_NAME must be set." >&2
  exit 1
fi

push_remote="${CI_REPOSITORY_URL:-origin}"
pipeline_commit="${CI_MERGE_REQUEST_SOURCE_BRANCH_SHA:-${CI_COMMIT_SHA:-$(git rev-parse HEAD)}}"

skip_if_remote_branch_changed() {
  local remote_commit

  if ! remote_commit="$(
    git ls-remote --exit-code --refs "$push_remote" \
      "refs/heads/$PUSH_BRANCH" | awk 'NR == 1 { print $1 }'
  )"; then
    echo "Unable to read remote branch $PUSH_BRANCH; refusing to run Codex or push." >&2
    exit 1
  fi

  if [[ -z "$remote_commit" ]]; then
    echo "Remote branch $PUSH_BRANCH has no commit; refusing to run Codex or push." >&2
    exit 1
  fi

  if [[ "$remote_commit" != "$pipeline_commit" ]]; then
    echo "Remote branch $PUSH_BRANCH changed after this pipeline was created."
    echo "Pipeline commit: $pipeline_commit"
    echo "Remote commit:   $remote_commit"
    echo "Skipping Codex, commit, and push; a newer pipeline should process the latest commit."
    exit 0
  fi
}

if [[ "$CODEX_AUTH" != /* ]]; then
  CODEX_AUTH="${CI_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/$CODEX_AUTH"
fi

if [[ -f "$CODEX_AUTH" ]]; then
  cp -- "$CODEX_AUTH" /root/.codex/auth.json
fi

printf 'CI_COMMIT_BRANCH=%s\n' "${CI_COMMIT_BRANCH:-}"
printf 'CI_PIPELINE_SOURCE=%s\n' "${CI_PIPELINE_SOURCE:-}"
printf 'CI_COMMIT_BEFORE_SHA=%s\n' "${CI_COMMIT_BEFORE_SHA:-}"
printf 'CI_COMMIT_SHA=%s\n' "${CI_COMMIT_SHA:-}"
printf 'CI_MERGE_REQUEST_CHANGED_FILES=%s\n' \
  "${CI_MERGE_REQUEST_CHANGED_FILES:-}"
printf 'TYPE=%s\n' "${TYPE:-}"
printf 'FILES=%s\n' "${FILES:-}"
printf 'DATE=%s\n' "${DATE:-}"
printf 'PUSH_COMMENT=%s\n' "${PUSH_COMMENT:-}"
printf 'PUSH_BRANCH=%s\n' "${PUSH_BRANCH:-}"
printf 'SELECTOR_FILE=%s\n' "$SELECTOR_FILE"
printf 'CODEX_ACTION=%s\n' "$CODEX_ACTION"
printf 'RESULTS_ID=%s\n' "$RESULTS_ID"

case "$CODEX_ACTION" in
  auto|reviewed) ;;
  *)
    echo "Invalid CODEX_ACTION: $CODEX_ACTION (expected auto or reviewed)" >&2
    exit 1
    ;;
esac

echo "Starting AI CICD Security Guardrail..."

if [[ -n "$RESULTS_ID" ]]; then
  if [[ ! "$RESULTS_ID" =~ ^[0-9a-f]{32}$ ]]; then
    echo "Invalid RESULTS_ID: expected 32 lowercase hexadecimal characters" >&2
    exit 1
  fi

  report_url="${CODE_QC_URL%/}/report?results_id=${RESULTS_ID}"
  websocket_url="${CODE_QC_WS_URL%/}/?results_id=${RESULTS_ID}"
else
  case "$TYPE" in
    syntax|suggestion|enhancement|all) ;;
    *)
      echo "Invalid TYPE: $TYPE (expected syntax, suggestion, enhancement, or all)" >&2
      exit 1
      ;;
  esac

  effective_files="$FILES"

  case "$effective_files" in
    all|changed_only) ;;
    *)
      echo "Invalid FILES: $effective_files (expected all or changed_only)" >&2
      exit 1
      ;;
  esac

  file_list="$(mktemp)"

  if [[ "$effective_files" == "all" ]]; then
    git ls-files -z > "$file_list"
  elif [[ ! -z "${CI_MERGE_REQUEST_CHANGED_FILES:-}" ]]; then
    # Accept either a JSON array or one path per line.
    if jq -e 'type == "array" and all(.[]; type == "string")' \
      >/dev/null 2>&1 <<< "$CI_MERGE_REQUEST_CHANGED_FILES"; then
      jq -j '.[] | ., "\u0000"' \
        <<< "$CI_MERGE_REQUEST_CHANGED_FILES" > "$file_list"
    else
      while IFS= read -r changed_file || [[ -n "$changed_file" ]]; do
        [[ -n "$changed_file" ]] && printf '%s\0' "$changed_file"
      done <<< "$CI_MERGE_REQUEST_CHANGED_FILES" > "$file_list"
    fi
  elif [[ "${CI_PIPELINE_SOURCE:-}" == "push" ]] &&
       [[ -n "${CI_COMMIT_BEFORE_SHA:-}" ]] &&
       [[ ! "$CI_COMMIT_BEFORE_SHA" =~ ^0+$ ]]; then
    # Match the range GitLab uses for rules:changes in a push pipeline. This
    # includes all commits in a multi-commit push, but excludes older changes
    # made on the source branch.
    if ! git cat-file -e "${CI_COMMIT_BEFORE_SHA}^{commit}" 2>/dev/null; then
      git fetch --no-tags "${CI_REPOSITORY_URL:-origin}" "$CI_COMMIT_BEFORE_SHA"
    fi
    git diff --name-only --diff-filter=ACMRTUXB -z \
      "$CI_COMMIT_BEFORE_SHA" "${CI_COMMIT_SHA:-HEAD}" > "$file_list"
  else
    # CI_COMMIT_BEFORE_SHA is all zeroes for new branches and pipelines that
    # are not created by a push. Preserve the branch-wide fallback for them.
    default_branch="${CI_DEFAULT_BRANCH:-main}"
    git fetch --no-tags "${CI_REPOSITORY_URL:-origin}" \
      "$default_branch:refs/remotes/origin/$default_branch"
    git diff --name-only --diff-filter=ACMRTUXB -z \
      "origin/$default_branch...HEAD" > "$file_list"
  fi

  # The same path can occur in the base selection and one or more working-tree
  # selections. De-duplicate it before counting files or creating an archive.
  unique_file_list="$(mktemp)"
  declare -A unique_paths=()
  while IFS= read -r -d '' project_file; do
    if [[ -z "${unique_paths[$project_file]+selected}" ]]; then
      unique_paths["$project_file"]=1
      printf '%s\0' "$project_file" >> "$unique_file_list"
    fi
  done < "$file_list"
  file_list="$unique_file_list"

  # Ignore paths that were deleted after the changed-file list was produced.
  existing_file_list="$(mktemp)"
  while IFS= read -r -d '' project_file; do
    if [[ -f "$project_file" ]]; then
      printf '%s\0' "$project_file" >> "$existing_file_list"
    fi
  done < "$file_list"

  candidate_count=0
  while IFS= read -r -d '' project_file; do
    ((candidate_count += 1))
  done < "$existing_file_list"

  # A multi-file upload includes only paths selected by Git-ignore-style
  # patterns in SELECTOR_FILE. Fall back to a source/text allowlist if absent.
  if (( candidate_count > 1 )); then
    selector_file="$SELECTOR_FILE"
    if [[ ! -f "$selector_file" ]]; then
      echo "No $SELECTOR_FILE found; using the default source/text patterns." >&2
      echo "Consider adding a $SELECTOR_FILE file to customize archive contents." >&2
      selector_file="$(mktemp)"
      printf '%s\n' \
        '*.py' '*.html' '*.htm' '*.css' '*.scss' \
        '*.js' '*.jsx' '*.ts' '*.tsx' '*.vue' '*.svelte' \
        '*.java' '*.c' '*.h' '*.cc' '*.cpp' '*.cxx' '*.hpp' \
        '*.cs' '*.go' '*.rs' '*.rb' '*.php' '*.swift' \
        '*.kt' '*.kts' '*.scala' '*.sh' '*.bash' '*.zsh' \
        '*.sql' '*.md' '*.txt' '*.rst' '*.yaml' '*.yml' \
        '*.json' '*.toml' '*.xml' 'Dockerfile*' 'Makefile' \
        > "$selector_file"
    fi

    declare -A selected_paths=()
    while IFS= read -r -d '' selected_file; do
      selected_paths["$selected_file"]=1
    done < <(
      git ls-files -z --cached --others --ignored \
        --exclude-from="$selector_file"
    )

    filtered_file_list="$(mktemp)"
    while IFS= read -r -d '' project_file; do
      if [[ -n "${selected_paths[$project_file]+selected}" ]]; then
        printf '%s\0' "$project_file" >> "$filtered_file_list"
      fi
    done < "$existing_file_list"
    existing_file_list="$filtered_file_list"
  fi

  file_count=0
  upload_file=""
  while IFS= read -r -d '' project_file; do
    ((file_count += 1))
    upload_file="$project_file"
  done < "$existing_file_list"

  if (( file_count == 0 )); then
    echo "No files found to submit to AI CICD Security Guardrail." >&2
    exit 1
  elif (( file_count > 1 )); then
    archive_dir="$(mktemp -d)"
    upload_file="$archive_dir/codeqc-files.tar.gz"
    tar --null --verbatim-files-from -czf "$upload_file" \
      --files-from="$existing_file_list"
  fi

  upload_size="$(du -h -- "$upload_file" | awk '{print $1}')"
  echo "Selected file count: $file_count"
  echo "Upload size: $upload_size"
  echo "Submitting to AI CICD Security Guardrail (type=$TYPE)."
  curl_prompt_args=()
  if [[ -n "$PROMPT" ]]; then
    curl_prompt_args+=(--form-string "user_prompt=$PROMPT")
  elif [[ -n "$PROMPT_FILE" ]]; then
    project_dir="$(realpath -e -- "${CI_PROJECT_DIR:-$(git rev-parse --show-toplevel)}")"
    if [[ "$PROMPT_FILE" == /* ]]; then
      prompt_file="$PROMPT_FILE"
    else
      prompt_file="$project_dir/$PROMPT_FILE"
    fi

    if [[ -f "$prompt_file" ]]; then
      prompt_file="$(realpath -e -- "$prompt_file")"
      case "$prompt_file" in
        "$project_dir"/*)
          curl_prompt_args+=(--form "prompt=@${prompt_file}")
          ;;
        *)
          printf '[WARN] PROMPT_FILE must be under CI_PROJECT_DIR; ignoring %s.\n' \
            "$PROMPT_FILE" >&2
          ;;
      esac
    else
      printf '[WARN] PROMPT_FILE is not a file; ignoring %s.\n' \
        "$PROMPT_FILE" >&2
    fi
  fi

  response="$(
    curl --fail-with-body --silent --show-error \
      --request POST \
      --form "file=@${upload_file}" \
      "${curl_prompt_args[@]}" \
      "${CODE_QC_URL%/}/api/codeqc?type=${TYPE}"
  )"

  report_path="$(jq -er '.report_url' <<< "$response")"
  websocket_url="$(jq -er '.websocket_url' <<< "$response")"

  case "$report_path" in
    http://*|https://*) report_url="$report_path" ;;
    *) report_url="${CODE_QC_URL%/}/${report_path#/}" ;;
  esac
fi

echo "AI CICD Security Guardrail report: $report_url"
echo "Monitoring AI CICD Security Guardrail status: $websocket_url"

monitor_args=("$websocket_url")
if [[ "$CODEX_ACTION" == "reviewed" ]]; then
  monitor_args+=(--status "reviewed")
fi

if npx /opt/codeqc-ws-monitor "${monitor_args[@]}"; then
  if [[ "$CODEX_ACTION" == "reviewed" ]]; then
    echo "AI CICD Security Guardrail manual review completed."
  else
    echo "AI CICD Security Guardrail completed."
  fi
  if [[ "$PUSH_BRANCH" == "main" ]]; then
    echo "PUSH_BRANCH is main; skipping Codex to protect the main branch from automatic changes."
    exit 0
  elif [[ -f "$CODEX_AUTH" ]]; then
    skip_if_remote_branch_changed
    echo "Codex is reviewing the report and applying fixes."
    codex_error_log="$(mktemp)"
    codex_prompt="please help to read the SRAA from $report_url
and help to fix in this workspace"
    if [[ "$CODEX_ACTION" == "reviewed" ]]; then
      codex_prompt+=$'\nOnly handle review points whose checkbox is checked. Ignore all unchecked points.'
    fi
    if codex exec --json --skip-git-repo-check \
      "$codex_prompt" 2>"$codex_error_log" \
      | jq --unbuffered -r '
          if .type == "item.completed" and .item.type == "reasoning" then
            "Thinking: \(.item.text)"
          elif .type == "item.completed" and .item.type == "agent_message" then
            .item.text
          elif .type == "item.started" and .item.type == "command_execution" then
            "Using tool: shell — \(.item.command)"
          elif .type == "item.started" and .item.type == "mcp_tool_call" then
            "Using tool: \(.item.server)/\(.item.tool)"
          elif .type == "item.started" and .item.type == "web_search" then
            "Using tool: web search"
          elif .type == "item.completed" and .item.type == "file_change" then
            "Updated files: \([.item.changes[]?.path] | join(", "))"
          elif .type == "turn.completed" then
            "Token usage: input=\(.usage.input_tokens // 0), cached=\(.usage.cached_input_tokens // 0), output=\(.usage.output_tokens // 0)"
          elif .type == "turn.failed" then
            "Codex turn failed: \(.error.message // "unknown error")"
          else empty
          end
        '; then
      echo "Codex finished applying the AI CICD Security Guardrail fixes."
      rm -f -- "$codex_error_log"
    else
      codex_status=$?
      echo "Codex failed (exit status $codex_status)." >&2
      cat "$codex_error_log" >&2
      rm -f -- "$codex_error_log"
      exit "$codex_status"
    fi
  else
    printf '[WARN] CODEX_AUTH(%s) is not exists, skipped the codex correction.' "$(realpath -m -- "$CODEX_AUTH")" >&2
    exit 0
  fi
else
  monitor_status=$?
  echo "AI CICD Security Guardrail WebSocket monitor failed (exit status $monitor_status)." >&2
  exit "$monitor_status"
fi

git config user.name "$PUSH_USERNAME"
git config user.email "$PUSH_EMAIL"
git add --all

if git diff --cached --quiet; then
  echo "Codex made no changes; nothing to commit or push."
  exit 0
fi

skip_if_remote_branch_changed
git commit -m "$PUSH_COMMENT"
git push -o ci.skip "$push_remote" "HEAD:refs/heads/$PUSH_BRANCH"
echo "Pushed Codex changes to $PUSH_BRANCH."
