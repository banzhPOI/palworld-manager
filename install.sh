#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 sudo 运行：sudo ./install.sh" >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "缺少 python3" >&2; exit 1; }
command -v docker >/dev/null || { echo "缺少 docker" >&2; exit 1; }
docker compose version >/dev/null || { echo "缺少 Docker Compose v2" >&2; exit 1; }

install_dir=/opt/palworld-manager
config_file=/etc/default/palworld-manager
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

install -d -m 0755 "${install_dir}"
install -m 0755 "${script_dir}/palworld_manager.py" "${install_dir}/palworld_manager.py"
install -m 0644 "${script_dir}/index.html" "${install_dir}/index.html"
install -m 0644 "${script_dir}/palworld-manager.service" /etc/systemd/system/palworld-manager.service

if [[ ! -f ${config_file} ]]; then
  install -m 0644 "${script_dir}/palworld-manager.env.example" "${config_file}"
fi

systemctl daemon-reload
systemctl enable --now palworld-manager.service
echo "Palworld Manager 已安装。请检查 ${config_file}，然后访问 http://服务器IP:8080"
