#!/usr/bin/env python3
"""Palworld 配置管理器：零第三方依赖的单文件 Web 服务。"""

import argparse
import base64
import datetime as dt
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


LOG = logging.getLogger("palworld-manager")
PALWORLD_ROOT = Path(os.getenv("PALWORLD_ROOT", "/opt/palworld"))
DATA_DIR = Path(os.getenv("PALWORLD_MANAGER_DATA", "/opt/palworld-manager"))
INI_PATH = Path(
    os.getenv(
        "PALWORLD_INI_PATH",
        str(PALWORLD_ROOT / "Saved/Config/LinuxServer/PalWorldSettings.ini"),
    )
)
COMPOSE_FILE = Path(os.getenv("PALWORLD_COMPOSE_FILE", str(PALWORLD_ROOT / "compose.yaml")))
COMPOSE_SERVICE = os.getenv("PALWORLD_COMPOSE_SERVICE", "palworld-server")
PAL_API_BASE = os.getenv("PALWORLD_REST_API", "http://127.0.0.1:8212/v1/api").rstrip("/")
STATE_PATH = DATA_DIR / "settings.json"
MEMBER_PATH = DATA_DIR / "members.json"
BACKUP_DIR = DATA_DIR / "backups"
WEB_PATH = Path(__file__).with_name("index.html")
STATE_LOCK = threading.RLock()
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
DISPLAY_TIMEZONE = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))


GROUPS = [
    "服务器管理",
    "性能配置",
    "功能开关",
    "游戏平衡",
    "保留与未分类",
]


def field(label: str, group: str, kind: str = "number", description: str = "", **kwargs: Any) -> Dict[str, Any]:
    result = {"label": label, "group": group, "type": kind, "description": description}
    result.update(kwargs)
    return result


FIELDS: Dict[str, Dict[str, Any]] = {
    "ServerName": field("服务器名称", "服务器", "text", "显示在服务器列表中的名称", maxLength=80),
    "ServerDescription": field("服务器简介", "服务器", "text", "服务器列表中展示的说明", maxLength=200),
    "ServerPassword": field("进入密码", "服务器", "password", "留空表示无需密码", maxLength=64),
    "AdminPassword": field("管理员密码", "服务器", "password", "用于服务器管理命令", maxLength=64),
    "ServerPlayerMaxNum": field("最大玩家数", "服务器", "integer", "建议根据服务器内存控制人数", min=1, max=32, step=1),
    "PublicPort": field("游戏端口", "服务器", "integer", "必须与容器映射端口一致", min=1, max=65535, step=1),
    "PublicIP": field("公网 IP", "服务器", "text", "通常留空自动识别", maxLength=64),
    "Region": field("服务器区域", "服务器", "text", "可留空", maxLength=40),
    "CrossplayPlatforms": field(
        "跨平台支持",
        "服务器",
        "multi",
        "允许连接服务器的平台",
        options=["Steam", "Xbox", "PS5", "Mac"],
    ),
    "bShowPlayerList": field("显示玩家列表", "服务器", "boolean", "允许查询在线玩家列表"),
    "bIsShowJoinLeftMessage": field("加入离开提示", "服务器", "boolean", "玩家加入或离开时显示通知"),
    "ChatPostLimitPerMinute": field("每分钟聊天上限", "服务器", "integer", "限制单个玩家的聊天频率", min=1, max=300, step=1),
    "Difficulty": field("难度", "游戏进度", "select", "基础难度预设", options=["None", "Normal", "Hard"]),
    "ExpRate": field("经验倍率", "游戏进度", "number", "玩家与帕鲁获得经验的倍率", min=0.1, max=20, step=0.1),
    "PalCaptureRate": field("帕鲁捕获倍率", "游戏进度", "number", "数值越高越容易捕获", min=0.1, max=10, step=0.1),
    "PalSpawnNumRate": field("帕鲁刷新数量", "游戏进度", "number", "提高会显著增加服务器负载", min=0.1, max=5, step=0.1),
    "PalEggDefaultHatchingTime": field("巨大蛋孵化小时数", "游戏进度", "number", "设为 0 可立即孵化", min=0, max=240, step=0.5),
    "WorkSpeedRate": field("工作速度倍率", "游戏进度", "number", "帕鲁工作的速度倍率", min=0.1, max=20, step=0.1),
    "DayTimeSpeedRate": field("白天速度", "时间与生存", "number", "白天时间流逝速度", min=0.1, max=10, step=0.1),
    "NightTimeSpeedRate": field("夜晚速度", "时间与生存", "number", "夜晚时间流逝速度", min=0.1, max=10, step=0.1),
    "PlayerStomachDecreaceRate": field("玩家饥饿速度", "时间与生存", "number", "越低越不容易饥饿", min=0, max=10, step=0.1),
    "PlayerStaminaDecreaceRate": field("玩家体力消耗", "时间与生存", "number", "越低体力消耗越慢", min=0, max=10, step=0.1),
    "PlayerAutoHPRegeneRate": field("玩家生命恢复", "时间与生存", "number", "非睡眠状态的生命恢复倍率", min=0, max=10, step=0.1),
    "PlayerAutoHpRegeneRateInSleep": field("玩家睡眠恢复", "时间与生存", "number", "睡眠状态的生命恢复倍率", min=0, max=10, step=0.1),
    "PalStomachDecreaceRate": field("帕鲁饥饿速度", "时间与生存", "number", "越低越不容易饥饿", min=0, max=10, step=0.1),
    "PalStaminaDecreaceRate": field("帕鲁体力消耗", "时间与生存", "number", "越低体力消耗越慢", min=0, max=10, step=0.1),
    "PalAutoHPRegeneRate": field("帕鲁生命恢复", "时间与生存", "number", "帕鲁生命恢复倍率", min=0, max=10, step=0.1),
    "PalAutoHpRegeneRateInSleep": field("帕鲁睡眠恢复", "时间与生存", "number", "帕鲁睡眠时的生命恢复倍率", min=0, max=10, step=0.1),
    "PlayerDamageRateAttack": field("玩家攻击伤害", "战斗", "number", "玩家造成伤害的倍率", min=0.1, max=10, step=0.1),
    "PlayerDamageRateDefense": field("玩家承受伤害", "战斗", "number", "玩家受到伤害的倍率", min=0.1, max=10, step=0.1),
    "PalDamageRateAttack": field("帕鲁攻击伤害", "战斗", "number", "帕鲁造成伤害的倍率", min=0.1, max=10, step=0.1),
    "PalDamageRateDefense": field("帕鲁承受伤害", "战斗", "number", "帕鲁受到伤害的倍率", min=0.1, max=10, step=0.1),
    "bIsPvP": field("启用 PVP", "战斗", "boolean", "允许玩家之间进行 PVP"),
    "bEnablePlayerToPlayerDamage": field("玩家互相伤害", "战斗", "boolean", "允许玩家直接伤害其他玩家"),
    "bEnableFriendlyFire": field("友军伤害", "战斗", "boolean", "允许对友方造成伤害"),
    "DeathPenalty": field("死亡惩罚", "战斗", "select", "玩家死亡时掉落的内容", options=["None", "Item", "ItemAndEquipment", "All"]),
    "CollectionDropRate": field("采集掉落倍率", "资源与掉落", "number", "采集资源的掉落数量", min=0.1, max=20, step=0.1),
    "CollectionObjectHpRate": field("采集物生命倍率", "资源与掉落", "number", "矿石、树木等可采集物耐久", min=0.1, max=10, step=0.1),
    "CollectionObjectRespawnSpeedRate": field("采集物刷新速度", "资源与掉落", "number", "数值越高刷新越快", min=0.1, max=10, step=0.1),
    "EnemyDropItemRate": field("敌人掉落倍率", "资源与掉落", "number", "敌人掉落物品数量倍率", min=0.1, max=20, step=0.1),
    "ItemWeightRate": field("物品重量倍率", "资源与掉落", "number", "越低物品越轻", min=0, max=10, step=0.1),
    "DropItemAliveMaxHours": field("掉落物保留小时", "资源与掉落", "number", "地面掉落物的最长存在时间", min=0.1, max=240, step=0.5),
    "BaseCampMaxNum": field("世界据点总数", "据点与公会", "integer", "整个世界允许存在的据点数量", min=1, max=256, step=1),
    "BaseCampWorkerMaxNum": field("据点工作帕鲁数", "据点与公会", "integer", "单个据点最大工作帕鲁数量", min=1, max=50, step=1),
    "BaseCampMaxNumInGuild": field("每公会据点数", "据点与公会", "integer", "单个公会可建立的据点数量", min=1, max=20, step=1),
    "GuildPlayerMaxNum": field("公会最大人数", "据点与公会", "integer", "单个公会允许的玩家数", min=1, max=100, step=1),
    "BuildObjectDamageRate": field("建筑受伤倍率", "据点与公会", "number", "建筑受到攻击时的伤害倍率", min=0, max=10, step=0.1),
    "BuildObjectDeteriorationDamageRate": field("建筑劣化速度", "据点与公会", "number", "设为 0 可关闭建筑自然损耗", min=0, max=10, step=0.1),
    "bAutoResetGuildNoOnlinePlayers": field("自动清理离线公会", "据点与公会", "boolean", "长期无人上线时自动清理公会"),
    "AutoResetGuildTimeNoOnlinePlayers": field("离线公会清理小时", "据点与公会", "number", "启用自动清理后的等待时间", min=1, max=720, step=1),
    "bEnableInvaderEnemy": field("据点袭击", "世界规则", "boolean", "允许敌人袭击据点"),
    "bEnableFastTravel": field("快速传送", "世界规则", "boolean", "允许使用快速传送"),
    "bIsStartLocationSelectByMap": field("地图选择出生点", "世界规则", "boolean", "新角色可从地图选择初始位置"),
    "bExistPlayerAfterLogout": field("离线保留角色", "世界规则", "boolean", "玩家下线后角色仍留在世界"),
    "bEnableNonLoginPenalty": field("离线惩罚", "世界规则", "boolean", "长期不上线时据点会受到惩罚"),
    "bAllowClientMod": field("允许客户端模组", "世界规则", "boolean", "允许模组客户端连接"),
    "bIsUseBackupSaveData": field("启用备份存档", "世界规则", "boolean", "由游戏服务端保留备份存档"),
    "AutoSaveSpan": field("自动存档间隔", "世界规则", "number", "单位为秒", min=10, max=3600, step=10),
    "RCONEnabled": field("启用 RCON", "高级参数", "boolean", "远程控制接口；无需使用时保持关闭"),
    "RCONPort": field("RCON 端口", "高级参数", "integer", "仅在启用 RCON 时生效", min=1, max=65535, step=1),
    "RESTAPIEnabled": field("启用 REST API", "高级参数", "boolean", "官方管理 API；不要直接暴露到公网"),
    "RESTAPIPort": field("REST API 端口", "高级参数", "integer", "仅在启用 REST API 时生效", min=1, max=65535, step=1),
    "bUseAuth": field("启用服务认证", "高级参数", "boolean", "保持开启以使用官方认证机制"),
    "BanListURL": field("封禁列表地址", "高级参数", "text", "官方封禁列表 URL", maxLength=300),
}

# 以下元数据依据 Palworld Server Guide 1.0.0 翻译整理。
FIELDS.update(
    {
        "ItemContainerForceMarkDirtyInterval": field("容器强制同步间隔", "性能配置", "number", "容器界面打开时，强制重新同步的间隔秒数", step=0.1),
        "MaxBuildingLimitNum": field("单玩家建筑上限", "性能配置", "integer", "每位玩家允许建造的建筑数量；0 表示不限制", step=1),
        "PhysicsActiveDropItemMaxNum": field("物理掉落物上限", "性能配置", "integer", "允许启用物理效果的地面掉落物最大数量", step=1),
        "ServerReplicatePawnCullDistance": field("帕鲁同步距离", "性能配置", "number", "玩家周围同步帕鲁的距离，单位厘米", min=5000, max=15000, step=100),
        "bEnableBuildingPlayerUIdDisplay": field("建筑显示创建者 ID", "服务器管理", "boolean", "在建筑物上显示创建者的玩家 ID"),
        "LogFormatType": field("日志格式", "服务器管理", "select", "服务端日志输出格式", options=["Text", "Json"]),
        "bAllowEnhanceStat_Attack": field("允许强化攻击", "功能开关", "boolean", "允许玩家把属性点分配到攻击力"),
        "bAllowEnhanceStat_Health": field("允许强化生命", "功能开关", "boolean", "允许玩家把属性点分配到生命值"),
        "bAllowEnhanceStat_Stamina": field("允许强化耐力", "功能开关", "boolean", "允许玩家把属性点分配到耐力"),
        "bAllowEnhanceStat_Weight": field("允许强化负重", "功能开关", "boolean", "允许玩家把属性点分配到负重"),
        "bAllowEnhanceStat_WorkSpeed": field("允许强化工作速度", "功能开关", "boolean", "允许玩家把属性点分配到工作速度"),
        "bAllowGlobalPalboxExport": field("允许导出到全局帕鲁终端", "功能开关", "boolean", "允许把帕鲁保存到全局帕鲁终端"),
        "bAllowGlobalPalboxImport": field("允许从全局帕鲁终端导入", "功能开关", "boolean", "允许从全局帕鲁终端加载帕鲁"),
        "bBuildAreaLimit": field("限制特殊区域建造", "功能开关", "boolean", "禁止在快速传送点等特殊建筑附近建造"),
        "bCharacterRecreateInHardcore": field("硬核死亡后重建角色", "功能开关", "boolean", "硬核模式死亡后是否允许重新创建角色"),
        "bDisplayPvPItemNumOnWorldMap_BaseCamp": field("地图显示据点 PVP 物品", "功能开关", "boolean", "在地图上显示各据点的 PVP 专属物品数量"),
        "bDisplayPvPItemNumOnWorldMap_Player": field("地图显示玩家 PVP 物品", "功能开关", "boolean", "在地图上显示玩家位置及其 PVP 专属物品数量"),
        "bEnableFastTravelOnlyBaseCamp": field("仅据点间快速传送", "功能开关", "boolean", "把快速传送限制为据点之间"),
        "bEnableVoiceChat": field("启用语音聊天", "功能开关", "boolean", "启用游戏内语音聊天"),
        "bHardcore": field("启用硬核模式", "功能开关", "boolean", "死亡后无法重生，请谨慎开启"),
        "bInvisibleOtherGuildBaseCampAreaFX": field("显示其他公会据点边界", "功能开关", "boolean", "控制其他公会据点范围边界的显示"),
        "bIsRandomizerPalLevelRandom": field("完全随机帕鲁等级", "功能开关", "boolean", "开启后野生帕鲁等级完全随机；关闭则按区域等级范围随机"),
        "RandomizerSeed": field("随机化种子", "功能开关", "text", "帕鲁刷新随机模式使用的种子", maxLength=128),
        "RandomizerType": field("帕鲁刷新随机模式", "功能开关", "select", "无随机、按区域随机或完全随机", options=["None", "Region", "All"]),
        "VoiceChatMaxVolumeDistance": field("语音全音量距离", "功能开关", "number", "语音聊天在该距离内不会衰减", step=100),
        "VoiceChatZeroVolumeDistance": field("语音静音距离", "功能开关", "number", "超过该距离后语音音量降为零", step=100),
        "AdditionalDropItemNumWhenPlayerKillingInPvPMode": field("PVP 击杀额外掉落数量", "游戏平衡", "integer", "启用 PVP 击杀额外掉落时的物品数量", step=1),
        "AdditionalDropItemWhenPlayerKillingInPvPMode": field("PVP 击杀额外掉落物品", "游戏平衡", "text", "启用额外掉落时使用的物品 ID", maxLength=128),
        "bAdditionalDropItemWhenPlayerKillingInPvPMode": field("启用 PVP 击杀额外掉落", "游戏平衡", "boolean", "PVP 模式击杀玩家时掉落指定物品"),
        "BlockRespawnTime": field("死亡重生冷却", "游戏平衡", "number", "玩家死亡后允许重生前的等待秒数", step=1),
        "bPalLost": field("死亡永久失去帕鲁", "游戏平衡", "boolean", "玩家死亡后永久失去队伍中的帕鲁"),
        "DenyTechnologyList": field("禁用科技列表", "游戏平衡", "raw", "填写原始列表，例如 (\"PALBOX\",\"RepairBench\")"),
        "EquipmentDurabilityDamageRate": field("装备耐久损耗倍率", "游戏平衡", "number", "装备耐久度损耗倍率", step=0.1),
        "GuildRejoinCooldownMinutes": field("重新加入公会冷却", "游戏平衡", "integer", "退出公会后重新加入的冷却分钟数", step=1),
        "ItemCorruptionMultiplier": field("物品腐坏速度", "游戏平衡", "number", "物品腐坏速度倍率", step=0.1),
        "MonsterFarmActionSpeedRate": field("牧场生产速度", "游戏平衡", "number", "帕鲁在牧场生产物品的速度倍率", step=0.1),
        "RespawnPenaltyDurationThreshold": field("连续死亡判定时间", "游戏平衡", "number", "在该生存时间阈值内再次死亡时应用额外重生冷却，单位秒", step=1),
        "RespawnPenaltyTimeScale": field("连续死亡冷却倍率", "游戏平衡", "number", "连续死亡时重生冷却的倍率", step=0.1),
        "SupplyDropSpan": field("陨石与补给间隔", "游戏平衡", "integer", "陨石或补给掉落的间隔分钟数", step=1),
    }
)

PERFORMANCE_KEYS = {
    "BaseCampMaxNum", "BaseCampMaxNumInGuild", "BaseCampWorkerMaxNum", "ItemContainerForceMarkDirtyInterval",
    "MaxBuildingLimitNum", "PhysicsActiveDropItemMaxNum", "ServerReplicatePawnCullDistance",
}
SERVER_KEYS = {
    "AdminPassword", "bAllowClientMod", "bEnableBuildingPlayerUIdDisplay", "bIsShowJoinLeftMessage",
    "bIsUseBackupSaveData", "ChatPostLimitPerMinute", "CrossplayPlatforms", "LogFormatType", "PublicIP",
    "PublicPort", "RCONEnabled", "RCONPort", "RESTAPIEnabled", "RESTAPIPort", "ServerDescription",
    "ServerName", "ServerPassword", "ServerPlayerMaxNum",
}
FEATURE_KEYS = {
    "AutoResetGuildTimeNoOnlinePlayers", "bAllowEnhanceStat_Attack", "bAllowEnhanceStat_Health",
    "bAllowEnhanceStat_Stamina", "bAllowEnhanceStat_Weight", "bAllowEnhanceStat_WorkSpeed",
    "bAllowGlobalPalboxExport", "bAllowGlobalPalboxImport", "bAutoResetGuildNoOnlinePlayers", "bBuildAreaLimit",
    "bCharacterRecreateInHardcore", "bDisplayPvPItemNumOnWorldMap_BaseCamp", "bDisplayPvPItemNumOnWorldMap_Player",
    "bEnableFastTravel", "bEnableFastTravelOnlyBaseCamp", "bEnableInvaderEnemy", "bEnableVoiceChat",
    "bExistPlayerAfterLogout", "bHardcore", "bInvisibleOtherGuildBaseCampAreaFX", "bIsPvP",
    "bIsRandomizerPalLevelRandom", "bIsStartLocationSelectByMap", "bShowPlayerList", "RandomizerSeed",
    "RandomizerType", "VoiceChatMaxVolumeDistance", "VoiceChatZeroVolumeDistance",
}
BALANCE_KEYS = {
    "AdditionalDropItemNumWhenPlayerKillingInPvPMode", "AdditionalDropItemWhenPlayerKillingInPvPMode",
    "bAdditionalDropItemWhenPlayerKillingInPvPMode", "BlockRespawnTime", "bPalLost", "BuildObjectDamageRate",
    "BuildObjectDeteriorationDamageRate", "CollectionDropRate", "CollectionObjectHpRate",
    "CollectionObjectRespawnSpeedRate", "DayTimeSpeedRate", "DeathPenalty", "DenyTechnologyList",
    "EnemyDropItemRate", "EquipmentDurabilityDamageRate", "ExpRate", "GuildPlayerMaxNum",
    "GuildRejoinCooldownMinutes", "ItemCorruptionMultiplier", "ItemWeightRate", "MonsterFarmActionSpeedRate",
    "NightTimeSpeedRate", "PalAutoHPRegeneRate", "PalAutoHpRegeneRateInSleep", "PalCaptureRate",
    "PalDamageRateAttack", "PalDamageRateDefense", "PalEggDefaultHatchingTime", "PalSpawnNumRate",
    "PalStaminaDecreaceRate", "PalStomachDecreaceRate", "PlayerAutoHPRegeneRate",
    "PlayerAutoHpRegeneRateInSleep", "PlayerDamageRateAttack", "PlayerDamageRateDefense",
    "PlayerStaminaDecreaceRate", "PlayerStomachDecreaceRate", "RespawnPenaltyDurationThreshold",
    "RespawnPenaltyTimeScale", "SupplyDropSpan",
}
DOCUMENTED_KEYS = PERFORMANCE_KEYS | SERVER_KEYS | FEATURE_KEYS | BALANCE_KEYS
for documented_key in DOCUMENTED_KEYS:
    if documented_key in FIELDS:
        if documented_key in PERFORMANCE_KEYS:
            FIELDS[documented_key]["group"] = "性能配置"
        elif documented_key in SERVER_KEYS:
            FIELDS[documented_key]["group"] = "服务器管理"
        elif documented_key in FEATURE_KEYS:
            FIELDS[documented_key]["group"] = "功能开关"
        else:
            FIELDS[documented_key]["group"] = "游戏平衡"

# 官方未声明范围的参数不施加人为上限，避免拒绝服务端支持的值。
for field_key, field_meta in FIELDS.items():
    if field_meta["type"] in ("integer", "number"):
        field_meta.pop("min", None)
        field_meta.pop("max", None)
for field_key, limits in {
    "BaseCampMaxNumInGuild": {"max": 10},
    "BaseCampWorkerMaxNum": {"max": 50},
    "ServerReplicatePawnCullDistance": {"min": 5000, "max": 15000},
    "PublicPort": {"min": 1, "max": 65535},
    "RCONPort": {"min": 1, "max": 65535},
    "RESTAPIPort": {"min": 1, "max": 65535},
}.items():
    FIELDS[field_key].update(limits)

ARGUMENT_FIELDS = [
    field("监听端口", "启动参数", "integer", "对应官方 -port；修改后会同步更新 Docker UDP 端口映射", key="port", min=1, max=65535, step=1),
    field("启动参数最大人数", "启动参数", "optionalInteger", "对应 -players；留空则使用 INI 中的 ServerPlayerMaxNum", key="players", min=1, step=1),
    field("传统多线程参数组", "启动参数", "boolean", "对应 -useperfthreads、-NoAsyncLoadingThread、-UseMultithreadForDS。官方说明 v1.0 以后不设置可能性能更好", key="performanceFlags"),
    field("工作线程数", "启动参数", "optionalInteger", "对应 -NumberOfWorkerThreadsServer；需与传统多线程参数组一起使用，官方建议不超过 CPU 线程数减 1", key="workerThreads", min=1, step=1),
    field("社区服务器", "启动参数", "boolean", "对应 -publiclobby，使服务器出现在社区服务器列表", key="publicLobby"),
    field("社区服务器公网 IP", "启动参数", "text", "对应 -publicip；通常留空自动检测", key="publicIp", maxLength=64),
    field("社区服务器公网端口", "启动参数", "optionalInteger", "对应 -publicport；不会改变实际监听端口", key="publicPort", min=1, max=65535, step=1),
    field("启动日志格式", "启动参数", "select", "对应 -logformat", key="logFormat", options=["text", "json"]),
]


def split_settings(content: str) -> List[str]:
    """按逗号拆分 OptionSettings，忽略引号和内层括号中的逗号。"""
    parts: List[str] = []
    start = 0
    quoted = False
    escaped = False
    depth = 0
    for index, char in enumerate(content):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(content[start:index].strip())
            start = index + 1
    tail = content[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def extract_option_settings(text: str) -> str:
    marker = "OptionSettings=("
    start = text.find(marker)
    if start < 0:
        raise ValueError("配置文件中未找到 OptionSettings")
    start += len(marker)
    quoted = False
    escaped = False
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return text[start:index]
            depth -= 1
    raise ValueError("OptionSettings 括号不完整")


def unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def parse_value(key: str, raw: str) -> Any:
    meta = FIELDS.get(key)
    if not meta:
        return raw
    kind = meta["type"]
    if kind in ("text", "password", "select"):
        return unquote(raw)
    if kind == "boolean":
        return raw.lower() == "true"
    if kind == "integer":
        return int(float(raw))
    if kind == "number":
        return float(raw)
    if kind == "multi":
        value = raw.strip()
        if value.startswith("(") and value.endswith(")"):
            value = value[1:-1]
        return [item.strip() for item in value.split(",") if item.strip()]
    return raw


def parse_ini(text: str) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for item in split_settings(extract_option_settings(text)):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        key = key.strip()
        if KEY_PATTERN.fullmatch(key):
            values[key] = parse_value(key, raw.strip())
    if not values:
        raise ValueError("配置文件中没有可识别的参数")
    return values


def quote(value: Any) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return '"{}"'.format(escaped)


def format_value(key: str, value: Any) -> str:
    meta = FIELDS.get(key)
    if not meta:
        return str(value)
    kind = meta["type"]
    if kind in ("text", "password"):
        return quote(value)
    if kind == "select":
        return str(value)
    if kind == "boolean":
        return "True" if value else "False"
    if kind == "integer":
        return str(int(value))
    if kind == "number":
        return "{:.6f}".format(float(value))
    if kind == "multi":
        return "({})".format(",".join(value))
    return str(value)


def render_ini(values: Dict[str, Any]) -> str:
    options = ",".join("{}={}".format(key, format_value(key, value)) for key, value in values.items())
    return (
        "; Generated by Palworld Manager. Manual changes may be overwritten.\n"
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=({})\n".format(options)
    )


def normalize_values(values: Any) -> Dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("values 必须是 JSON 对象")
    if not values or len(values) > 300:
        raise ValueError("配置项数量不合法")
    normalized: Dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise ValueError("配置项名称不合法：{}".format(key))
        meta = FIELDS.get(key)
        if not meta:
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError("高级参数 {} 必须是原始字符串".format(key))
            normalized[key] = value
            continue
        kind = meta["type"]
        try:
            if kind in ("text", "password"):
                value = str(value)
                if len(value) > meta.get("maxLength", 300):
                    raise ValueError("内容过长")
            elif kind == "select":
                value = str(value)
                if value not in meta["options"]:
                    raise ValueError("不支持的选项")
            elif kind == "boolean":
                if not isinstance(value, bool):
                    raise ValueError("必须是布尔值")
            elif kind == "integer":
                if isinstance(value, bool):
                    raise ValueError("必须是整数")
                value = int(value)
            elif kind == "number":
                if isinstance(value, bool):
                    raise ValueError("必须是数字")
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("必须是有限数字")
            elif kind == "multi":
                if not isinstance(value, list) or not value:
                    raise ValueError("至少选择一个平台")
                invalid = [item for item in value if item not in meta["options"]]
                if invalid:
                    raise ValueError("包含不支持的平台")
                value = list(dict.fromkeys(value))
            elif kind == "raw":
                if not isinstance(value, str) or len(value) > 1000:
                    raise ValueError("必须是原始参数字符串")
            if kind in ("integer", "number"):
                if "min" in meta and value < meta["min"]:
                    raise ValueError("不能小于 {}".format(meta["min"]))
                if "max" in meta and value > meta["max"]:
                    raise ValueError("不能大于 {}".format(meta["max"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("{}：{}".format(meta["label"], exc))
        normalized[key] = value
    return normalized


def read_ini_values() -> Dict[str, Any]:
    return parse_ini(INI_PATH.read_text(encoding="utf-8-sig"))


DEFAULT_ARGUMENTS: Dict[str, Any] = {
    "port": 8211,
    "players": None,
    "performanceFlags": False,
    "workerThreads": None,
    "publicLobby": False,
    "publicIp": "",
    "publicPort": None,
    "logFormat": "text",
}


def parse_compose_arguments(text: str) -> Dict[str, Any]:
    arguments = dict(DEFAULT_ARGUMENTS)
    command_indent: Optional[int] = None
    raw_arguments: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if command_indent is None:
            if stripped == "command:":
                command_indent = indent
            continue
        if stripped and indent <= command_indent:
            break
        match = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if match:
            raw_arguments.append(match.group(1).strip().strip('"').strip("'"))
    for argument in raw_arguments:
        lower = argument.lower()
        if lower.startswith("-port="):
            arguments["port"] = int(argument.split("=", 1)[1])
        elif lower.startswith("-players="):
            arguments["players"] = int(argument.split("=", 1)[1])
        elif argument in ("-useperfthreads", "-NoAsyncLoadingThread", "-UseMultithreadForDS"):
            arguments["performanceFlags"] = True
        elif lower.startswith("-numberofworkerthreadsserver="):
            arguments["workerThreads"] = int(argument.split("=", 1)[1])
        elif lower == "-publiclobby":
            arguments["publicLobby"] = True
        elif lower.startswith("-publicip="):
            arguments["publicIp"] = argument.split("=", 1)[1]
        elif lower.startswith("-publicport="):
            arguments["publicPort"] = int(argument.split("=", 1)[1])
        elif lower.startswith("-logformat="):
            arguments["logFormat"] = argument.split("=", 1)[1].lower()
    return arguments


def normalize_arguments(arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments 必须是 JSON 对象")
    normalized = dict(DEFAULT_ARGUMENTS)
    normalized.update({key: arguments.get(key) for key in DEFAULT_ARGUMENTS if key in arguments})
    try:
        normalized["port"] = int(normalized["port"])
        if not 1 <= normalized["port"] <= 65535:
            raise ValueError("监听端口必须在 1 到 65535 之间")
        for key, label in (("players", "最大人数"), ("workerThreads", "工作线程数"), ("publicPort", "公网端口")):
            value = normalized[key]
            if value in (None, ""):
                normalized[key] = None
            else:
                normalized[key] = int(value)
                if normalized[key] < 1:
                    raise ValueError("{}必须大于 0".format(label))
        if normalized["publicPort"] is not None and normalized["publicPort"] > 65535:
            raise ValueError("公网端口必须在 1 到 65535 之间")
        for key in ("performanceFlags", "publicLobby"):
            if not isinstance(normalized[key], bool):
                raise ValueError("启动参数开关必须是布尔值")
        normalized["publicIp"] = str(normalized["publicIp"] or "")
        if len(normalized["publicIp"]) > 64:
            raise ValueError("公网 IP 内容过长")
        normalized["logFormat"] = str(normalized["logFormat"]).lower()
        if normalized["logFormat"] not in ("text", "json"):
            raise ValueError("日志格式只能是 text 或 json")
        if normalized["workerThreads"] is not None and not normalized["performanceFlags"]:
            raise ValueError("设置工作线程数前必须开启传统多线程参数组")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("监听", "最大", "工作", "公网", "启动", "日志", "设置")):
            raise
        raise ValueError("启动参数格式错误：{}".format(exc))
    return normalized


def read_compose_arguments() -> Dict[str, Any]:
    return normalize_arguments(parse_compose_arguments(COMPOSE_FILE.read_text(encoding="utf-8")))


def render_compose_arguments(text: str, arguments: Dict[str, Any]) -> str:
    arguments = normalize_arguments(arguments)
    command_values = ["-port={}".format(arguments["port"])]
    if arguments["players"] is not None:
        command_values.append("-players={}".format(arguments["players"]))
    if arguments["performanceFlags"]:
        command_values.extend(["-useperfthreads", "-NoAsyncLoadingThread", "-UseMultithreadForDS"])
        if arguments["workerThreads"] is not None:
            command_values.append("-NumberOfWorkerThreadsServer={}".format(arguments["workerThreads"]))
    if arguments["publicLobby"]:
        command_values.append("-publiclobby")
    if arguments["publicIp"]:
        command_values.append("-publicip={}".format(arguments["publicIp"]))
    if arguments["publicPort"] is not None:
        command_values.append("-publicport={}".format(arguments["publicPort"]))
    command_values.append("-logformat={}".format(arguments["logFormat"]))

    lines = text.splitlines()
    start = None
    end = None
    indent = 0
    for index, line in enumerate(lines):
        if line.strip() == "command:":
            start = index
            indent = len(line) - len(line.lstrip())
            continue
        if start is not None and index > start and line.strip() and len(line) - len(line.lstrip()) <= indent:
            end = index
            break
    if start is None:
        raise ValueError("Compose 文件中未找到 command 配置")
    if end is None:
        end = len(lines)
    prefix = " " * (indent + 2)
    replacement = [lines[start]] + ["{}- {}".format(prefix, item) for item in command_values]
    lines[start:end] = replacement

    port_pattern = re.compile(r'^(\s*-\s*["\']?)(\d+):(\d+)/udp(["\']?\s*)$')
    replaced_port = False
    for index, line in enumerate(lines):
        match = port_pattern.match(line)
        if match and (match.group(2) == str(DEFAULT_ARGUMENTS["port"]) or match.group(3) == str(DEFAULT_ARGUMENTS["port"])):
            port = str(arguments["port"])
            lines[index] = "{}{}:{}/udp{}".format(match.group(1), port, port, match.group(4))
            replaced_port = True
            break
    if not replaced_port:
        LOG.warning("未自动更新 Docker UDP 端口映射，请确认 Compose ports 配置")
    return "\n".join(lines) + "\n"


def render_rest_api_mapping(text: str, enabled: bool, port: int) -> str:
    """仅把 REST API 暴露到宿主机回环地址，避免直接进入局域网或公网。"""
    marker = "# Palworld Manager local REST API"
    lines = [line for line in text.splitlines() if marker not in line]
    if not enabled:
        return "\n".join(lines) + "\n"
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("REST API 端口必须在 1 到 65535 之间")
    ports_index = None
    ports_indent = 0
    insert_index = None
    for index, line in enumerate(lines):
        if line.strip() == "ports:":
            ports_index = index
            ports_indent = len(line) - len(line.lstrip())
            continue
        if ports_index is not None and index > ports_index:
            if line.strip() and len(line) - len(line.lstrip()) <= ports_indent:
                insert_index = index
                break
    if ports_index is None:
        raise ValueError("Compose 文件中未找到 ports 配置")
    if insert_index is None:
        insert_index = len(lines)
    mapping = '{0}- "127.0.0.1:{1}:{1}/tcp" {2}'.format(" " * (ports_indent + 2), port, marker)
    lines.insert(insert_index, mapping)
    return "\n".join(lines) + "\n"


def save_state(values: Dict[str, Any], arguments: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 2,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "values": values,
        "arguments": arguments,
    }
    atomic_write(STATE_PATH, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_state() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    with STATE_LOCK:
        if STATE_PATH.exists():
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if payload.get("schemaVersion", 1) < 2:
                values = read_ini_values()
                arguments = read_compose_arguments()
                save_state(values, arguments)
                return values, arguments
            values = normalize_values(payload.get("values"))
            arguments = normalize_arguments(payload.get("arguments") or read_compose_arguments())
            if "arguments" not in payload:
                save_state(values, arguments)
            return values, arguments
        values = read_ini_values()
        arguments = read_compose_arguments()
        save_state(values, arguments)
        return values, arguments


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=".{}-".format(path.name), dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing:
            os.chmod(temporary_path, existing.st_mode)
            try:
                os.chown(temporary_path, existing.st_uid, existing.st_gid)
            except PermissionError:
                pass
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_members() -> List[Dict[str, Any]]:
    """读取管理端见过的全部成员，在线状态由实时接口覆盖。"""
    with STATE_LOCK:
        if not MEMBER_PATH.exists():
            return []
        try:
            payload = json.loads(MEMBER_PATH.read_text(encoding="utf-8"))
            members = payload.get("members", []) if isinstance(payload, dict) else []
            return [member for member in members if isinstance(member, dict) and member.get("userId")]
        except (OSError, json.JSONDecodeError):
            LOG.exception("读取成员档案失败")
            return []


def save_members(members: List[Dict[str, Any]]) -> None:
    payload = {
        "schemaVersion": 1,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "members": members,
    }
    atomic_write(MEMBER_PATH, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def merge_online_members(players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并在线快照并持久化，使玩家离线后仍可管理。"""
    with STATE_LOCK:
        known = {str(item["userId"]): dict(item) for item in load_members()}
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        online_ids = set()
        for player in players:
            user_id = str(player.get("userId") or "").strip()
            if not user_id:
                continue
            online_ids.add(user_id)
            record = known.get(user_id, {})
            for key in (
                "name", "accountName", "playerId", "userId", "ping",
                "location_x", "location_y", "level", "building_count",
            ):
                if player.get(key) is not None:
                    record[key] = player[key]
            ip_address = player.get("ip") or player.get("iP")
            if ip_address:
                record["ip"] = ip_address
            record["userId"] = user_id
            record["lastSeenAt"] = now
            record.setdefault("firstSeenAt", now)
            record.setdefault("banned", False)
            known[user_id] = record
        persisted = sorted(known.values(), key=lambda item: item.get("lastSeenAt", ""), reverse=True)
        save_members(persisted)
        result = []
        for record in persisted:
            item = dict(record)
            item["online"] = str(item.get("userId")) in online_ids
            result.append(item)
        return result


def set_member_banned(user_id: str, banned: bool) -> None:
    with STATE_LOCK:
        members = load_members()
        found = False
        for member in members:
            if str(member.get("userId")) == user_id:
                member["banned"] = banned
                found = True
                break
        if not found:
            members.append({"userId": user_id, "name": user_id, "banned": banned})
        save_members(members)


def backup_ini() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / "PalWorldSettings-{}.ini".format(stamp)
    shutil.copy2(str(INI_PATH), str(target))
    os.utime(target, None)
    return target


def create_world_archive(target: Optional[Path] = None) -> Path:
    """导出世界存档、服务配置和管理端档案。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if target is None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = DATA_DIR / "palworld-export-{}.zip".format(stamp)
    saved_path = PALWORLD_ROOT / "Saved"
    if not saved_path.is_dir():
        raise RuntimeError("未找到世界存档目录")
    manifest = {
        "format": "palworld-manager-archive-v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "serverName": read_ini_values().get("ServerName", "palworld"),
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.write(COMPOSE_FILE, "config/compose.yaml")
        if STATE_PATH.exists():
            archive.write(STATE_PATH, "manager/settings.json")
        if MEMBER_PATH.exists():
            archive.write(MEMBER_PATH, "manager/members.json")
        for path in saved_path.rglob("*"):
            if path.is_file():
                archive.write(path, str(Path("Saved") / path.relative_to(saved_path)))
    return target


def validate_world_archive(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > 200000:
            raise ValueError("压缩包文件数量过多")
        total_size = 0
        for info in infos:
            member = Path(info.filename)
            if member.is_absolute() or ".." in member.parts or info.filename.startswith(("/", "\\")):
                raise ValueError("压缩包包含不安全路径")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("压缩包不能包含符号链接")
            total_size += info.file_size
        if total_size > 64 * 1024 * 1024 * 1024:
            raise ValueError("解压后数据超过 64 GB")
        names = {info.filename for info in infos}
        if "manifest.json" not in names or not any(name.startswith("Saved/") for name in names):
            raise ValueError("不是有效的配置与存档导出包")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != "palworld-manager-archive-v1":
            raise ValueError("不支持的导出包版本")
        return manifest


def import_world_archive(path: Path) -> Dict[str, Any]:
    """校验并导入存档；失败时恢复原数据并重新启动服务。"""
    manifest = validate_world_archive(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="palworld-import-", dir=str(DATA_DIR)))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    saved_path = PALWORLD_ROOT / "Saved"
    previous_saved = PALWORLD_ROOT / ".Saved-before-import-{}".format(stamp)
    previous_compose = COMPOSE_FILE.read_bytes()
    stopped = False
    swapped = False
    try:
        with zipfile.ZipFile(path, "r") as archive:
            archive.extractall(staging)
        imported_saved = staging / "Saved"
        if not imported_saved.is_dir():
            raise ValueError("导出包缺少 Saved 目录")
        success, detail = service_action("stop")
        if not success:
            raise RuntimeError("停止游戏服务失败：{}".format(detail))
        stopped = True
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = create_world_archive(BACKUP_DIR / "pre-import-{}.zip".format(stamp))
        shutil.move(str(saved_path), str(previous_saved))
        shutil.move(str(imported_saved), str(saved_path))
        swapped = True
        imported_compose = staging / "config/compose.yaml"
        if imported_compose.is_file():
            atomic_write(COMPOSE_FILE, imported_compose.read_text(encoding="utf-8"))
        success, detail = service_action("start")
        if not success:
            raise RuntimeError("导入后启动游戏服务失败：{}".format(detail))
        stopped = False
        shutil.rmtree(previous_saved)
        values = read_ini_values()
        save_state(values, read_compose_arguments())
        return {"message": "配置与世界存档导入成功", "backup": backup.name, "manifest": manifest, "status": service_status()}
    except Exception:
        if swapped:
            failed_saved = PALWORLD_ROOT / ".Saved-failed-import-{}".format(stamp)
            if saved_path.exists():
                shutil.move(str(saved_path), str(failed_saved))
            if previous_saved.exists():
                shutil.move(str(previous_saved), str(saved_path))
            if failed_saved.exists():
                shutil.rmtree(failed_saved)
            atomic_write(COMPOSE_FILE, previous_compose.decode("utf-8"))
        if stopped:
            service_action("start")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def compose_command(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    command = ["docker", "compose", "-f", str(COMPOSE_FILE)] + list(args)
    LOG.info("执行固定服务命令：%s", " ".join(command))
    return subprocess.run(
        command,
        cwd=str(PALWORLD_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def docker_command(*args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker"] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def service_status() -> Dict[str, Any]:
    try:
        result = compose_command("ps", "--status", "running", "-q", COMPOSE_SERVICE, timeout=10)
        container_id = result.stdout.strip() if result.returncode == 0 else ""
        running = bool(container_id)
        status: Dict[str, Any] = {"running": running, "state": "运行中" if running else "已停止"}
        if container_id:
            inspect = docker_command("inspect", container_id, timeout=10)
            if inspect.returncode == 0:
                data = json.loads(inspect.stdout)[0]
                status.update(
                    {
                        "container": data.get("Name", "").lstrip("/"),
                        "image": data.get("Config", {}).get("Image", ""),
                        "startedAt": data.get("State", {}).get("StartedAt", ""),
                        "restartCount": data.get("RestartCount", 0),
                    }
                )
            stats = docker_command("stats", "--no-stream", "--format", "{{json .}}", container_id, timeout=10)
            if stats.returncode == 0 and stats.stdout.strip():
                metrics = json.loads(stats.stdout.strip().splitlines()[0])
                status["cpu"] = metrics.get("CPUPerc", "—")
                status["memory"] = metrics.get("MemUsage", "—")
        return status
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"running": False, "state": "状态未知", "detail": str(exc)}


def service_action(action: str) -> Tuple[bool, str]:
    commands = {
        "start": ("up", "-d", COMPOSE_SERVICE),
        "stop": ("stop", COMPOSE_SERVICE),
        "restart": ("restart", COMPOSE_SERVICE),
        "recreate": ("up", "-d", "--force-recreate", COMPOSE_SERVICE),
    }
    if action not in commands:
        return False, "不支持的服务操作"
    try:
        result = compose_command(*commands[action], timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = result.stdout.strip()
    if result.returncode != 0:
        return False, output or "服务操作执行失败"
    return True, output or "服务操作已完成"


def recent_logs(lines: int = 160) -> str:
    lines = max(20, min(lines, 500))
    result = compose_command("logs", "--no-color", "--timestamps", "--tail", str(lines), COMPOSE_SERVICE, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(result.stdout.strip() or "读取日志失败")
    timestamp_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

    def localize(match: re.Match) -> str:
        raw = re.sub(r"(\.\d{6})\d+Z$", r"\1Z", match.group(0))
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(DISPLAY_TIMEZONE).isoformat(timespec="seconds")

    converted = timestamp_pattern.sub(localize, result.stdout)
    engine_pattern = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")

    def localize_engine(match: re.Match) -> str:
        parsed = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return "[{}]".format(parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"))

    return engine_pattern.sub(localize_engine, converted)


def pal_api_request(path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Any:
    values = read_ini_values()
    if not values.get("RESTAPIEnabled"):
        raise RuntimeError("官方 REST API 尚未启用")
    password = str(values.get("AdminPassword") or "")
    if not password:
        raise RuntimeError("请先设置管理员密码才能使用官方 REST API")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(PAL_API_BASE + path, data=body, method=method)
    token = base64.b64encode(("admin:" + password).encode("utf-8")).decode("ascii")
    request.add_header("Authorization", "Basic " + token)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            content = response.read()
            if not content:
                return {}
            return json.loads(content.decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == 401:
            raise RuntimeError("官方 REST API 认证失败，请检查管理员密码")
        raise RuntimeError("官方 REST API 返回 {}：{}".format(exc.code, detail or exc.reason))
    except URLError as exc:
        raise RuntimeError("无法连接官方 REST API：{}".format(exc.reason))


def pal_overview() -> Dict[str, Any]:
    try:
        players = pal_api_request("/players").get("players", [])
        return {
            "available": True,
            "info": pal_api_request("/info"),
            "metrics": pal_api_request("/metrics"),
            "players": players,
            "members": merge_online_members(players),
        }
    except RuntimeError as exc:
        members = [dict(member, online=False) for member in load_members()]
        return {"available": False, "error": str(exc), "info": {}, "metrics": {}, "players": [], "members": members}


def validate_user_id(value: Any) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_:-]+", value):
        raise ValueError("玩家用户 ID 不合法")
    return value


def pal_action(payload: Dict[str, Any]) -> str:
    action = str(payload.get("action") or "")
    if action == "save":
        pal_api_request("/save", "POST", {})
        return "世界存档已保存"
    if action == "announce":
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 500:
            raise ValueError("公告内容不能为空且不能超过 500 个字符")
        pal_api_request("/announce", "POST", {"message": message})
        return "全服公告已发送"
    if action in ("kick", "ban"):
        user_id = validate_user_id(payload.get("userid"))
        message = str(payload.get("message") or "管理员操作")[:200]
        pal_api_request("/" + action, "POST", {"userid": user_id, "message": message})
        if action == "ban":
            set_member_banned(user_id, True)
        return "玩家已{}".format("踢出" if action == "kick" else "封禁")
    if action == "unban":
        user_id = validate_user_id(payload.get("userid"))
        pal_api_request("/unban", "POST", {"userid": user_id})
        set_member_banned(user_id, False)
        return "玩家已解除封禁"
    raise ValueError("不支持的官方 API 操作")


def enable_pal_api() -> Dict[str, Any]:
    values = read_ini_values()
    arguments = read_compose_arguments()
    values["RESTAPIEnabled"] = True
    values["RESTAPIPort"] = int(values.get("RESTAPIPort") or 8212)
    if not values.get("AdminPassword"):
        values["AdminPassword"] = secrets.token_urlsafe(24)
    result = apply_values(values, arguments)
    result["message"] = "官方 REST API 已启用并限制在本机回环地址"
    return result


def apply_values(values: Dict[str, Any], arguments: Dict[str, Any]) -> Dict[str, Any]:
    with STATE_LOCK:
        values = normalize_values(values)
        arguments = normalize_arguments(arguments)
        backup = backup_ini()
        compose_backup = BACKUP_DIR / "compose-{}.yaml".format(dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(str(COMPOSE_FILE), str(compose_backup))
        previous_state = STATE_PATH.read_text(encoding="utf-8") if STATE_PATH.exists() else None
        try:
            atomic_write(INI_PATH, render_ini(values))
            compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
            compose_text = render_compose_arguments(compose_text, arguments)
            compose_text = render_rest_api_mapping(
                compose_text,
                bool(values.get("RESTAPIEnabled")),
                int(values.get("RESTAPIPort") or 8212),
            )
            atomic_write(COMPOSE_FILE, compose_text)
            save_state(values, arguments)
            success, detail = service_action("recreate")
            if not success:
                raise RuntimeError("服务重启失败：{}".format(detail))
        except Exception as exc:
            shutil.copy2(str(backup), str(INI_PATH))
            shutil.copy2(str(compose_backup), str(COMPOSE_FILE))
            if previous_state is None:
                if STATE_PATH.exists():
                    STATE_PATH.unlink()
            else:
                atomic_write(STATE_PATH, previous_state)
            service_action("recreate")
            LOG.exception("应用配置失败，已恢复原配置")
            if isinstance(exc, RuntimeError):
                raise RuntimeError("{}，已恢复原配置".format(exc))
            raise
    return {"message": "配置与启动参数已应用，帕鲁服务已重新创建", "backup": backup.name, "status": service_status()}


def build_fields(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for key in values:
        meta = dict(FIELDS.get(key) or field(key, "保留与未分类", "raw", "官方当前文档未说明的原始参数，修改前请确认版本兼容性"))
        if key not in DOCUMENTED_KEYS:
            meta["group"] = "保留与未分类"
            meta["description"] = "官方 1.0.0 文档未列出的保留、废弃或未分类参数"
        meta["key"] = key
        result.append(meta)
    return result


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Palworld 配置管理</title>
  <style>
    :root{--bg:#07131d;--panel:#0d202d;--panel2:#112b3a;--line:#21485b;--text:#e7f7ff;--muted:#88aebe;--cyan:#50e5e5;--blue:#6ba8ff;--gold:#ffd773;--danger:#ff7e84;--shadow:0 20px 60px rgba(0,0,0,.28)}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 75% -15%,#163e52 0,transparent 38%),linear-gradient(135deg,#061019,#0a1b27 58%,#07131d);color:var(--text);font:14px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;min-height:100vh}
    button,input,select{font:inherit}.shell{max-width:1480px;margin:auto;padding:32px}.top{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;margin-bottom:28px}.eyebrow{font-size:12px;letter-spacing:.22em;color:var(--cyan);font-weight:800}.title{font-size:34px;line-height:1.15;margin:8px 0 6px;letter-spacing:-.03em}.subtitle{color:var(--muted);margin:0}.status{display:flex;align-items:center;gap:10px;background:rgba(13,32,45,.82);border:1px solid var(--line);border-radius:16px;padding:12px 16px;box-shadow:var(--shadow)}.dot{width:10px;height:10px;border-radius:50%;background:var(--muted);box-shadow:0 0 0 5px rgba(136,174,190,.12)}.status.on .dot{background:#52e59b;box-shadow:0 0 0 5px rgba(82,229,155,.12)}
    .layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:22px}.side,.main{background:rgba(13,32,45,.82);backdrop-filter:blur(18px);border:1px solid var(--line);box-shadow:var(--shadow)}.side{border-radius:20px;padding:14px;position:sticky;top:18px;height:max-content}.nav{display:grid;gap:5px}.nav button{border:0;background:transparent;color:var(--muted);text-align:left;padding:11px 13px;border-radius:11px;cursor:pointer;transition:.18s}.nav button:hover{background:#133042;color:var(--text)}.nav button.active{background:linear-gradient(100deg,rgba(80,229,229,.2),rgba(107,168,255,.12));color:var(--cyan);box-shadow:inset 3px 0 var(--cyan)}
    .sideNote{border-top:1px solid var(--line);margin-top:14px;padding:15px 10px 4px;color:var(--muted);font-size:12px}.main{border-radius:20px;overflow:hidden}.toolbar{display:flex;gap:12px;align-items:center;padding:18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(13,32,45,.94);backdrop-filter:blur(20px);z-index:5}.search{flex:1;position:relative}.search input{width:100%;background:#071924;border:1px solid var(--line);border-radius:12px;color:var(--text);padding:11px 14px 11px 40px;outline:none}.search input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(80,229,229,.1)}.search:before{content:"⌕";position:absolute;left:14px;top:6px;font-size:22px;color:var(--muted)}
    .btn{border:1px solid var(--line);border-radius:11px;padding:10px 15px;color:var(--text);background:#102a39;cursor:pointer;white-space:nowrap}.btn:hover{border-color:#3d7188}.btn.primary{background:linear-gradient(100deg,#2f8fb1,#397cce);border-color:#66cadd;font-weight:700}.btn:disabled{opacity:.5;cursor:wait}.dirty{display:none;color:var(--gold);font-size:12px}.dirty.show{display:block}
    .content{padding:22px}.sectionHead{display:flex;justify-content:space-between;align-items:end;margin:0 0 16px}.sectionHead h2{margin:0;font-size:21px}.sectionHead span{color:var(--muted);font-size:12px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.card{background:linear-gradient(145deg,rgba(17,43,58,.95),rgba(10,29,41,.95));border:1px solid #1b4255;border-radius:15px;padding:16px;min-height:126px;transition:.18s}.card:focus-within{border-color:#4ca8c3;box-shadow:0 0 0 3px rgba(80,229,229,.08)}.cardTop{display:flex;justify-content:space-between;gap:12px}.label{font-weight:700;font-size:15px}.key{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#6192a7;overflow-wrap:anywhere}.desc{color:var(--muted);font-size:12px;margin:5px 0 12px;min-height:19px}.control input,.control select{width:100%;border:1px solid #285066;border-radius:10px;background:#071924;color:var(--text);padding:10px 11px;outline:none}.control input[type=number]{font-variant-numeric:tabular-nums}.toggle{display:flex;align-items:center;gap:10px;cursor:pointer;width:max-content}.toggle input{display:none}.track{width:46px;height:25px;border-radius:20px;background:#233f4e;padding:3px;transition:.2s}.thumb{width:19px;height:19px;border-radius:50%;background:#9fb8c4;transition:.2s}.toggle input:checked+.track{background:#258aa0}.toggle input:checked+.track .thumb{transform:translateX(21px);background:#d9ffff}.multi{display:flex;flex-wrap:wrap;gap:8px}.chip{display:flex;gap:6px;align-items:center;border:1px solid #285066;background:#071924;border-radius:9px;padding:7px 9px}.chip input{accent-color:var(--cyan)}
    .empty{padding:60px 20px;text-align:center;color:var(--muted)}.toast{position:fixed;right:24px;bottom:24px;max-width:420px;background:#123246;border:1px solid #387089;border-radius:13px;padding:13px 16px;box-shadow:var(--shadow);transform:translateY(120px);opacity:0;transition:.25s;z-index:20}.toast.show{transform:translateY(0);opacity:1}.toast.error{border-color:#8c434b;color:#ffdfe1}.loading{opacity:.55;pointer-events:none}
    @media(max-width:920px){.shell{padding:18px}.top{display:block}.status{margin-top:18px;width:max-content}.layout{grid-template-columns:1fr}.side{position:static;overflow:auto}.nav{display:flex;min-width:max-content}.sideNote{display:none}.grid{grid-template-columns:1fr}.toolbar{flex-wrap:wrap}.search{min-width:100%;order:-1}.title{font-size:28px}.content{padding:16px}}
  </style>
</head>
<body>
<div class="shell">
  <header class="top">
    <div><div class="eyebrow">PALWORLD SERVER CONTROL</div><h1 class="title">世界参数控制台</h1><p class="subtitle">调整世界规则，保存后应用到专用服务器。</p></div>
    <div id="status" class="status"><span class="dot"></span><div><strong id="statusText">正在检查</strong><div class="key">当前主机 · UDP 8211</div></div></div>
  </header>
  <div class="layout">
    <aside class="side"><nav id="nav" class="nav"></nav><div class="sideNote">没有账号系统，仅建议在可信局域网中访问。应用配置时会自动备份原始 INI。</div></aside>
    <main id="main" class="main">
      <div class="toolbar">
        <div class="search"><input id="search" placeholder="搜索配置名称或参数键…"></div>
        <span id="dirty" class="dirty">● 有未应用修改</span>
        <button id="reload" class="btn">重新读取</button>
        <button id="save" class="btn">仅保存</button>
        <button id="apply" class="btn primary">保存并应用</button>
      </div>
      <div class="content"><div class="sectionHead"><h2 id="sectionTitle">配置</h2><span id="count"></span></div><div id="grid" class="grid"></div></div>
    </main>
  </div>
</div>
<div id="toast" class="toast"></div>
<script>
const state={groups:[],fields:[],values:{},active:'服务器',query:'',dirty:false,busy:false};
const $=s=>document.querySelector(s); const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.className='toast show'+(error?' error':'');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.className='toast',3600)}
function setBusy(value){state.busy=value;$('#main').classList.toggle('loading',value);document.querySelectorAll('button').forEach(b=>b.disabled=value)}
async function api(path,options){const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||`请求失败 (${response.status})`);return data}
async function load(reload=false){setBusy(true);try{const data=await api(reload?'/api/reload':'/api/config',{method:reload?'POST':'GET'});state.groups=data.groups;state.fields=data.fields;state.values=data.values;state.dirty=data.dirty||false;if(!state.groups.includes(state.active))state.active=state.groups[0];render();setStatus(data.status);if(reload)toast('已从当前 INI 重新读取配置')}catch(e){toast(e.message,true)}finally{setBusy(false)}}
function setStatus(status){const el=$('#status');el.classList.toggle('on',!!status.running);$('#statusText').textContent=status.state||'状态未知'}
function render(){renderNav();renderFields();$('#dirty').classList.toggle('show',state.dirty)}
function renderNav(){const nav=$('#nav');nav.innerHTML='';state.groups.forEach(group=>{const b=document.createElement('button');b.textContent=group;b.className=group===state.active?'active':'';b.onclick=()=>{state.active=group;render()};nav.appendChild(b)})}
function visibleFields(){const q=state.query.trim().toLowerCase();return state.fields.filter(f=>f.group===state.active&&(!q||`${f.label} ${f.key} ${f.description}`.toLowerCase().includes(q)))}
function control(f,v){const common=`data-key="${esc(f.key)}"`;if(f.type==='boolean')return `<label class="toggle"><input ${common} type="checkbox" ${v?'checked':''}><span class="track"><span class="thumb"></span></span><span>${v?'已开启':'已关闭'}</span></label>`;
if(f.type==='select')return `<select ${common}>${f.options.map(o=>`<option ${o===v?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
if(f.type==='multi')return `<div class="multi">${f.options.map(o=>`<label class="chip"><input ${common} data-option="${esc(o)}" type="checkbox" ${v.includes(o)?'checked':''}>${esc(o)}</label>`).join('')}</div>`;
const type=f.type==='integer'||f.type==='number'?'number':f.type==='password'?'password':'text';const attrs=type==='number'?`min="${f.min??''}" max="${f.max??''}" step="${f.step??'any'}"`:'';return `<input ${common} type="${type}" value="${esc(v)}" ${attrs}>`}
function renderFields(){const fields=visibleFields();$('#sectionTitle').textContent=state.active;$('#count').textContent=`${fields.length} 项`;const grid=$('#grid');if(!fields.length){grid.innerHTML='<div class="empty">没有匹配的配置项</div>';return}grid.innerHTML=fields.map(f=>`<article class="card"><div class="cardTop"><span class="label">${esc(f.label)}</span><span class="key">${esc(f.key)}</span></div><div class="desc">${esc(f.description||'')}</div><div class="control">${control(f,state.values[f.key])}</div></article>`).join('');grid.querySelectorAll('input,select').forEach(el=>el.addEventListener('change',changeValue));grid.querySelectorAll('input[type=text],input[type=password],input[type=number]').forEach(el=>el.addEventListener('input',changeValue))}
function changeValue(event){const el=event.target,key=el.dataset.key,field=state.fields.find(f=>f.key===key);if(field.type==='multi'){state.values[key]=[...document.querySelectorAll(`[data-key="${CSS.escape(key)}"][data-option]:checked`)].map(x=>x.dataset.option)}else if(field.type==='boolean'){state.values[key]=el.checked;el.closest('.toggle').lastElementChild.textContent=el.checked?'已开启':'已关闭'}else if(field.type==='integer'){state.values[key]=el.value===''?'':parseInt(el.value,10)}else if(field.type==='number'){state.values[key]=el.value===''?'':parseFloat(el.value)}else state.values[key]=el.value;state.dirty=true;$('#dirty').classList.add('show')}
async function save(apply=false){if(apply&&!confirm('将备份当前配置、写入新配置并重启帕鲁服务，继续吗？'))return;setBusy(true);try{const path=apply?'/api/apply':'/api/config';const method=apply?'POST':'PUT';const data=await api(path,{method,body:JSON.stringify({values:state.values})});state.dirty=!apply;$('#dirty').classList.toggle('show',state.dirty);if(data.status)setStatus(data.status);toast(data.message)}catch(e){toast(e.message,true)}finally{setBusy(false)}}
$('#search').addEventListener('input',e=>{state.query=e.target.value;renderFields()});$('#reload').onclick=()=>load(true);$('#save').onclick=()=>save(false);$('#apply').onclick=()=>save(true);load();setInterval(async()=>{try{setStatus(await api('/api/status'))}catch(_){}},15000);
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "PalworldManager/0.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.info("%s - %s", self.client_address[0], format_string % args)

    def send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024 * 1024:
            raise ValueError("请求内容为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    def read_archive_upload(self) -> Path:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 32 * 1024 * 1024 * 1024:
            raise ValueError("导入文件为空或超过 32 GB")
        descriptor, temporary = tempfile.mkstemp(prefix="palworld-upload-", suffix=".zip", dir=str(DATA_DIR))
        remaining = length
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("导入文件传输不完整")
                    handle.write(chunk)
                    remaining -= len(chunk)
            return Path(temporary)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def send_archive(self, path: Path) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(path.name))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = (WEB_PATH.read_text(encoding="utf-8") if WEB_PATH.exists() else HTML).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/config":
                values, arguments = load_state()
                actual = read_ini_values()
                self.send_json(
                    {
                        "groups": GROUPS,
                        "fields": build_fields(values),
                        "values": values,
                        "argumentFields": ARGUMENT_FIELDS,
                        "arguments": arguments,
                        "dirty": render_ini(values) != render_ini(actual) or arguments != read_compose_arguments(),
                        "status": service_status(),
                        "documentationVersion": "1.0.0",
                    }
                )
            elif parsed.path == "/api/status":
                self.send_json(service_status())
            elif parsed.path == "/api/logs":
                count = int(parse_qs(parsed.query).get("lines", ["160"])[0])
                self.send_json({"logs": recent_logs(count)})
            elif parsed.path == "/api/backups":
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                items = [
                    {"name": path.name, "size": path.stat().st_size, "modifiedAt": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat()}
                    for path in sorted(BACKUP_DIR.glob("PalWorldSettings-*.ini"), key=lambda item: item.stat().st_mtime, reverse=True)[:30]
                ]
                self.send_json({"items": items})
            elif parsed.path == "/api/pal/overview":
                self.send_json(pal_overview())
            elif parsed.path == "/api/archive/export":
                try:
                    pal_api_request("/save", "POST", {})
                except RuntimeError:
                    LOG.warning("导出前调用官方保存接口失败，继续导出磁盘现有数据")
                archive = create_world_archive()
                try:
                    self.send_archive(archive)
                finally:
                    archive.unlink(missing_ok=True)
            elif parsed.path == "/health":
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            LOG.exception("处理 GET 请求失败")
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        if self.path != "/api/config":
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json()
            values = normalize_values(payload.get("values"))
            arguments = normalize_arguments(payload.get("arguments"))
            with STATE_LOCK:
                save_state(values, arguments)
            self.send_json({"message": "配置草稿已保存到 settings.json"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.exception("保存配置失败")
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/reload":
                with STATE_LOCK:
                    values = read_ini_values()
                    arguments = read_compose_arguments()
                    save_state(values, arguments)
                self.send_json(
                    {
                        "groups": GROUPS,
                        "fields": build_fields(values),
                        "values": values,
                        "argumentFields": ARGUMENT_FIELDS,
                        "arguments": arguments,
                        "dirty": False,
                        "status": service_status(),
                        "documentationVersion": "1.0.0",
                    }
                )
            elif self.path == "/api/apply":
                payload = self.read_json()
                self.send_json(apply_values(payload.get("values"), payload.get("arguments")))
            elif self.path == "/api/service/action":
                action = self.read_json().get("action")
                success, detail = service_action(str(action))
                if not success:
                    raise RuntimeError(detail)
                self.send_json({"message": "服务器操作已完成", "detail": detail, "status": service_status()})
            elif self.path == "/api/backup":
                target = backup_ini()
                self.send_json({"message": "配置备份已创建", "backup": target.name})
            elif self.path == "/api/pal/action":
                message = pal_action(self.read_json())
                self.send_json({"message": message, "overview": pal_overview()})
            elif self.path == "/api/pal/enable":
                self.read_json()
                self.send_json(enable_pal_api())
            elif self.path == "/api/archive/import":
                upload = self.read_archive_upload()
                try:
                    self.send_json(import_world_archive(upload))
                finally:
                    upload.unlink(missing_ok=True)
            else:
                self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.exception("应用配置失败")
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description="轻量级 Palworld 配置管理网页")
    parser.add_argument("--bind", default=os.getenv("PALWORLD_MANAGER_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PALWORLD_MANAGER_PORT", "8080")))
    args = parser.parse_args()
    os.environ["TZ"] = os.getenv("TZ", "Asia/Shanghai")
    if hasattr(time, "tzset"):
        time.tzset()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_state()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    LOG.info("Palworld Manager 启动：http://%s:%s", args.bind, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
