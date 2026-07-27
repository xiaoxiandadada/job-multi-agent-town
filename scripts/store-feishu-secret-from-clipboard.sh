#!/bin/zsh
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 ENV_KEY" >&2
  exit 2
fi

key="$1"
case "$key" in
  LARK_ROLE_JOB_SCOUT_APP_SECRET|\
  LARK_ROLE_JD_ANALYST_APP_SECRET|\
  LARK_ROLE_JOB_KNOWLEDGE_CURATOR_APP_SECRET|\
  LARK_ROLE_RESUME_STRATEGIST_APP_SECRET|\
  LARK_ROLE_PORTFOLIO_COACH_APP_SECRET|\
  LARK_ROLE_INTERVIEW_COACH_APP_SECRET|\
  LARK_ROLE_JUDGE_APP_SECRET)
    ;;
  *)
    echo "Unsupported Feishu secret key" >&2
    exit 2
    ;;
esac

secret="$(pbpaste | tr -d '\r\n')"
if [[ ${#secret} -lt 8 || ${#secret} -gt 256 || "$secret" == *[[:space:]]* ]]; then
  echo "Clipboard does not contain a valid App Secret" >&2
  exit 1
fi
trap 'pbcopy </dev/null' EXIT

project_dir="${0:A:h:h}"
env_file="$project_dir/.env"
if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file" >&2
  exit 1
fi

app_id_key="${key%_SECRET}_ID"
app_id=""
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == "$app_id_key="* ]]; then
    app_id="${line#*=}"
    break
  fi
done <"$env_file"
if [[ "$app_id" != cli_* ]]; then
  echo "Missing or invalid $app_id_key" >&2
  exit 1
fi

if ! printf '%s' "$secret" \
  | "$project_dir/.venv/bin/python" \
      "$project_dir/scripts/verify_feishu_credential.py" "$app_id"; then
  echo "$key was not stored because credential verification failed" >&2
  exit 1
fi

umask 077
temp_file="$(mktemp "$project_dir/.env.secret.XXXXXX")"
trap 'rm -f "$temp_file"; pbcopy </dev/null' EXIT

found=0
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == "$key="* ]]; then
    printf '%s=%s\n' "$key" "$secret" >>"$temp_file"
    found=1
  else
    printf '%s\n' "$line" >>"$temp_file"
  fi
done <"$env_file"
if [[ "$found" -eq 0 ]]; then
  printf '%s=%s\n' "$key" "$secret" >>"$temp_file"
fi

mv "$temp_file" "$env_file"
chmod 600 "$env_file"
pbcopy </dev/null
trap - EXIT
echo "$key=stored"
