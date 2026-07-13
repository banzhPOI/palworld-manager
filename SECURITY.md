# Security Policy

## Supported versions

安全修复只保证应用到默认分支的最新版本。

## Reporting a vulnerability

请不要在公开 Issue 中提交管理员密码、玩家 IP、完整存档或可直接访问的服务器地址。
请通过仓库所有者公开的 GitHub 联系方式私下报告安全问题，并提供最小复现步骤。

## Deployment boundary

Palworld Manager 没有内置身份认证，设计目标是可信局域网。不要将管理端口直接暴露到公网。
建议通过 VPN 或带强认证的反向代理访问，并限制服务器上 Docker socket 和存档目录的权限。
