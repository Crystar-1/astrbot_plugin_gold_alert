# London Gold Alert - 伦敦金提醒插件

专为 AstrBot 开发的伦敦金实时价格监控与到价提醒插件，支持 QQ 私聊和群聊场景。

## 功能特性

| 特性         | 说明                       |
| ---------- | ------------------------ |
| 📊 实时监控    | 定时获取伦敦金现货价格              |
| 🔔 到价提醒    | 价格穿过设定值时自动通知             |
| 🔒 三重解锁    | 防止重复触发的锁定机制              |
| ⚡ iTick数据源 | 专业金融API，支持REST和WebSocket |
| 👥 权限控制    | 白名单 + 管理员分级权限            |
| 💾 数据持久化   | 插件重启后自动恢复                |

***

## 快速开始

### 1. 获取iTick API Token

1. 访问 [iTick官网](https://itick.org) 注册账号
2. 登录后在个人中心获取 API Token
3. 免费套餐即可满足基本使用

### 2. 安装插件

本插件所需依赖极少，直接安装即可使用

## 配置说明

### iTick 相关配置

默认即为伦敦金现货价格监控（美元/盎司）

| 参数          | 默认值    | 说明                     |
| ----------- | ------ | ---------------------- |
| iTick Token | 空      | **必填** API访问令牌         |
| iTick地区代码   | GB     | 外汇市场使用GB               |
| 贵金属代码       | XAUUSD | XAUUSD=黄金，XAGUSD=白银    |
| 使用WebSocket | false  | true=实时推送，false=REST轮询 |

### 监控配置

| 参数     | 默认值  | 范围        | 说明       |
| ------ | ---- | --------- | -------- |
| 查询间隔   | 12秒  | 5-300秒    | 价格查询频率   |
| 浮动范围   | 10美元 | 1-100美元   | 解锁触发范围   |
| 锁定时长   | 300秒 | 60-3600秒  | 防重复触发锁定  |
| 提醒发送间隔 | 5秒   | 1-60秒     | 重复提醒间隔   |
| 提醒总条数  | 3条   | 1-10条     | 每次触发的提醒数 |
| 重试次数   | 2次   | 0-5次      | API调用失败重试 |

### 权限配置

| 参数    | 默认值 | 说明              |
| ----- | --- | --------------- |
| 用户白名单 | 空   | 留空表示不限制，使用列表格式，如: [\"123456\", \"789012\"] |
| 管理员列表 | 空   | 管理员用户ID列表 |

***

## 指令列表

### 用户指令

| 指令               | 说明     | 示例               |
| ---------------- | ------ | ---------------- |
| `/gold price`    | 查询当前金价 | `/gold price`    |
| `/金价`            | 查询当前金价 | `/金价`            |
| `/gold add <价格>` | 添加价格提醒 | `/gold add 1900` |
| `/gold ls`       | 查看我的提醒 | `/gold ls`       |
| `/gold rm <价格>`  | 删除指定提醒 | `/gold rm 1900`  |
| `/gold rmall`    | 删除全部提醒 | `/gold rmall`    |

### 管理员指令

| 指令                      | 说明       | 示例                        |
| ----------------------- | -------- | ------------------------- |
| `/admgold list`         | 查看所有提醒   | `/admgold list`           |
| `/admgold rm <价格> <QQ>` | 删除指定用户提醒 | `/admgold rm 1900 123456` |
| `/admgold restart`      | 重启监控     | `/admgold restart`        |
| `/admgold stop`         | 停止监控     | `/admgold stop`           |

***

## 触发规则

### 「穿过」判定

**问题17修复：更新触发规则描述与代码实现一致**

- 上涨穿过：上一次价格 < 设定价 <= 当前价格
- 下跌穿过：上一次价格 > 设定价 >= 当前价格
- 触及不算：价格恰好等于设定价不触发（需要明显穿过）

### 防重复触发机制

触发后进入锁定状态，满足以下任一条件解锁：

1. 价格超出浮动范围（设定价 ± 浮动范围）
2. 锁定超时（达到锁定时长）
3. 用户主动删除提醒

***

## 数据存储

- 配置目录：`AstrBot/data/london_gold_alert/`
- 提醒数据：`gold_alerts.json`
- 卸载时可选是否删除数据

***

## iTick API

### 支持品种

| 代码     | 品种    |
| ------ | ----- |
| XAUUSD | 黄金兑美元 |
| XAGUSD | 白银兑美元 |

### REST API

```
GET https://api.itick.org/forex/quote?region=GB&code=XAUUSD
Headers:
  accept: application/json
  token: your_token
```

### WebSocket

```
wss://api.itick.org/forex
Headers:
  token: your_token
```

***

## 常见问题

**Q: 插件启动失败，提示"Token未配置"**

> 请在插件设置中填写 iTick API Token

**Q: 价格数据获取失败**

> 检查：Token是否有效、网络是否正常、品种代码是否正确

**Q: 如何更换交易品种？**

> 修改"贵金属交易代码"：XAUUSD（黄金）或 XAGUSD（白银）

***

## 项目结构

```
astrbot_plugin_london_gold_alert/
├── __init__.py          # 插件入口导出
├── main.py              # 插件主类、生命周期管理
├── api.py               # iTick API客户端
├── data.py              # 数据持久化管理
├── monitor.py           # 价格监控核心逻辑
├── commands.py          # 指令处理器
├── _conf_schema.json    # 配置参数定义
├── metadata.yaml        # 插件元信息
├── requirements.txt     # Python依赖
└── README.md            # 项目文档
```

***

## 依赖

- Python 3.9+
- aiohttp >= 3.9.0
- websocket-client >= 1.6.0（仅WebSocket模式需要）

***

## License

MIT
