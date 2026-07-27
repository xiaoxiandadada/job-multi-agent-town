#!/bin/zsh
set -euo pipefail

mode="${1:-status}"

case "$mode" in
  enable)
    echo "Enabling lid-closed system operation. Keep the Mac on power and ventilated."
    sudo pmset -a disablesleep 1
    pmset -g | grep disablesleep
    ;;
  disable)
    echo "Restoring normal lid sleep."
    sudo pmset -a disablesleep 0
    pmset -g | grep disablesleep
    ;;
  status)
    pmset -g | grep disablesleep || echo "disablesleep is not explicitly set"
    ;;
  *)
    echo "Usage: $0 [enable|disable|status]" >&2
    exit 2
    ;;
esac
