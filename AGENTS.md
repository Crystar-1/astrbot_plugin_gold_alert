# Gold Alert Plugin - Agent Instructions

## 项目概述
- AstrBot 插件，监控伦敦金实时价格，支持价格到价提醒
- 通过 iTick API 获取数据，支持 REST 轮询或 WebSocket 实时推送
- 支持 QQ 私聊和群聊场景

## 架构
- `main.py`: GoldAlert (Star 子类)，生命周期管理，指令路由
- `api.py`: GoldPriceAPI，iTick REST/WebSocket 客户端
- `monitor.py`: PriceMonitor，核心监控循环和触发逻辑
- `data.py`: DataManager，警告持久化，线程安全文件 I/O
- `commands.py`: GoldAlertCommands，指令处理器

## 已知 Bug（必须修复）

### 1. DataManager 线程安全缺陷
`DataManager.get_all_alerts()` (data.py:257-268) **没有加锁**就直接读取 `_data.alerts`，而其他方法都使用了 `DataManager._lock`。

### 2. commands.py 类型错误
Line 237: `alert.get("is_locked")` — `alert` 是 `AlertRule` dataclass 实例，不是 dict，应改为 `alert.is_locked`。

## 指令
- 用户: `/gold price`, `/gold add <价格>`, `/gold ls`, `/gold rm <价格>`, `/gold rmall`
- 管理员: `/admgold list`, `/admgold rm <价格> <用户ID>`, `/admgold restart`, `/admgold stop`

## 依赖
- aiohttp >= 3.9.0
- websocket-client >= 1.6.0（仅 WebSocket 模式需要）

## 实现细节
- 价格使用 `Decimal` 保证精度（ROUND_HALF_UP，保留2位小数）
- 触发条件：价格必须"穿过"设定价（严格 < 或 >），触及不算
- 锁定后价格超出浮动范围或锁定超时自动解锁
