#!/usr/bin/env bash
# 让常驻 Agent 在合上电脑后继续工作（macOS）。
#
# 合上盖子默认会让 macOS 进入睡眠，Python 进程随之挂起，常驻巡检就断了。
# 这个脚本做三件事：
#   1. 用 launchd 把 API 服务 + 常驻巡检注册成开机自启、崩溃自拉起的常驻服务；
#   2. 用 caffeinate 阻止系统/磁盘睡眠，让进程在合盖后继续跑；
#   3. 打印 pmset 的核对命令，方便确认设置真的生效。
#
# 前提：外接电源。macOS 在纯电池 + 合盖时仍可能强制睡眠，
# 这也是 docs/deployment.md 里推荐长期跑在 VPS/容器上的原因。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL_PREFIX="com.jobagent"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$PROJECT_ROOT/data/runtime/logs"
UV_BIN="$(command -v uv || true)"

if [[ -z "$UV_BIN" ]]; then
  echo "找不到 uv，请先安装：https://docs.astral.sh/uv/" >&2
  exit 1
fi

mkdir -p "$AGENTS_DIR" "$LOG_DIR"

write_plist() {
  local name="$1" script="$2"
  local label="$LABEL_PREFIX.$name"
  local plist="$AGENTS_DIR/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>-i</string>
    <string>$UV_BIN</string>
    <string>run</string>
    <string>$script</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/$name.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$name.err.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "已注册 $label（$plist）"
}

write_plist "api" "job-agent-api"
write_plist "watch" "job-agent-watch"
write_plist "feishu" "job-agent-feishu"

echo
echo "接下来让合盖不睡眠（需要 sudo，复用已有脚本）："
echo "  $PROJECT_ROOT/scripts/clamshell-work.sh enable"
echo "核对当前电源设置："
echo "  $PROJECT_ROOT/scripts/clamshell-work.sh status"
echo "查看常驻巡检状态："
echo "  curl -s localhost:8000/api/always-on | python3 -m json.tool"
echo "停掉服务："
echo "  launchctl bootout gui/$(id -u)/$LABEL_PREFIX.watch"
