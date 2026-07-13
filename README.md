# Palworld Manager

一个零运行时第三方依赖、面向 Linux + Docker Compose 的轻量级《幻兽帕鲁》专用服务器管理面板。界面采用 Raycast 风格，配置项提供中文名称和说明。

> 非 Pocketpair 官方项目。配置字段和服务器操作依据 [Palworld Server Guide](https://docs.palworldgame.com/) 整理。

## 功能

- 中文分类编辑 `PalWorldSettings.ini`，保留官方未识别字段。
- 管理官方启动参数，并同步 Docker UDP 端口映射。
- 启动、停止、重启游戏服务，查看容器资源与东八区日志。
- 查看在线玩家和历史成员，显示等级、整数 Ping、IP 与最近上线时间。
- 踢出、封禁、解封玩家，发送全服公告并立即保存世界。
- 查看 FPS、在线人数、游戏天数、据点数量和世界 GUID。
- 导出完整世界存档、INI、Compose 与成员档案为 ZIP。
- 导入前自动备份；导入失败自动回滚并恢复游戏服务。

## 适用环境

- Linux，建议 Ubuntu 22.04 或更高版本。
- Python 3.9+。
- Docker Engine 与 Docker Compose v2。
- 已通过 Compose 运行的 Palworld Dedicated Server。
- 游戏存档必须持久化到宿主机；不要只保存在容器可写层。

面板默认假定：

```text
/opt/palworld/
├── compose.yaml
└── Saved/
    └── Config/LinuxServer/PalWorldSettings.ini
```

Compose 服务名默认为 `palworld-server`。不同路径和服务名可通过环境变量修改。

## 快速安装

```bash
git clone https://github.com/banzhPOI/palworld-manager.git
cd palworld-manager
sudo bash ./install.sh
```

安装完成后访问：

```text
http://服务器IP:8080
```

安装脚本会：

1. 将程序安装到 `/opt/palworld-manager`。
2. 创建 `/etc/default/palworld-manager` 配置文件。
3. 安装并启动 `palworld-manager.service`。
4. 不修改或重启现有游戏容器。

## 配置

编辑 `/etc/default/palworld-manager`：

```bash
PALWORLD_ROOT=/opt/palworld
PALWORLD_COMPOSE_FILE=/opt/palworld/compose.yaml
PALWORLD_COMPOSE_SERVICE=palworld-server
PALWORLD_INI_PATH=/opt/palworld/Saved/Config/LinuxServer/PalWorldSettings.ini
PALWORLD_MANAGER_DATA=/opt/palworld-manager/data
PALWORLD_MANAGER_BIND=0.0.0.0
PALWORLD_MANAGER_PORT=8080
PALWORLD_REST_API=http://127.0.0.1:8212/v1/api
TZ=Asia/Shanghai
```

修改后重启管理服务：

```bash
sudo systemctl restart palworld-manager
```

### Compose 要求

管理端会调用：

```bash
docker compose -f /path/to/compose.yaml <command> palworld-server
```

因此运行管理端的用户必须有 Docker 权限。默认 systemd 服务以 root 运行。

游戏存档应使用 bind mount，例如：

```yaml
services:
  palworld-server:
    image: ghcr.io/pocketpairjp/palserver:latest
    restart: unless-stopped
    ports:
      - "8211:8211/udp"
    volumes:
      - ./Saved:/pal/Package/Pal/Saved
```

实际镜像启动命令请以 [Pocketpair 官方 Docker 项目](https://github.com/pocketpairjp/palworld-dedicated-server-docker) 为准。

## 启用在线管理

在线玩家、指标、公告、保存和封禁功能依赖官方 REST API。面板可一键启用：

- 自动设置 `RESTAPIEnabled=True`。
- `AdminPassword` 为空时生成随机密码。
- REST API 只映射到 `127.0.0.1`，不会直接暴露到局域网或公网。
- 启用过程会重新创建游戏容器，应在无人在线时执行。

官方 REST API 只返回当前在线玩家。面板会把见过的玩家保存在 `members.json`，供离线查看和管理。

## 导入与导出

导出包包含：

- `Saved/` 世界与玩家存档。
- `config/compose.yaml`。
- 管理端配置草稿与成员档案。
- 格式与创建时间清单。

导入时会停止游戏服，并在 `backups/` 下创建 `pre-import-*.zip`。压缩包会经过版本、大小、文件数量、符号链接和路径穿越校验。导入或启动失败时会恢复原数据。

## 升级

```bash
cd palworld-manager
git pull
sudo bash ./install.sh
```

`data/`、配置备份和成员档案不会被安装脚本覆盖。

## 查看状态与日志

```bash
systemctl status palworld-manager
journalctl -u palworld-manager -f
curl http://127.0.0.1:8080/health
```

## 卸载

```bash
sudo systemctl disable --now palworld-manager
sudo rm /etc/systemd/system/palworld-manager.service
sudo systemctl daemon-reload
```

如需保留备份，不要删除 `/opt/palworld-manager/data`。

## 安全

该面板当前没有账号系统，只适合可信局域网：

- 不要把 `8080/tcp` 暴露到公网。
- 如需远程访问，请放在 VPN、Tailscale、WireGuard 或带身份认证的反向代理之后。
- 不要把包含真实密码、玩家 IP 或存档的 `data/` 提交到 Git。
- 导入存档前确认没有玩家在线。

安全问题请参阅 [SECURITY.md](SECURITY.md)。

## 开发与测试

```bash
python3 -m py_compile palworld_manager.py
python3 -m unittest -v test_palworld_manager.py
```

运行时只使用 Python 标准库。

## License

[MIT](LICENSE)
