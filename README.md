# 习酒即时零售门店动销看板

当前部署版本：`V3.3.10`

该项目由一个 Python 标准库 HTTP 服务提供静态看板，并在同源路径 `/api/feishu/sync` 提供飞书在线表格同步接口。服务不依赖数据库，也不需要安装第三方 Python 包。

## 页面地址

- `/`：自动跳转到 `V3.3.10`
- `/xjdashboardV3.3.10.html`：看板
- `/healthz`：Railway 健康检查

## Railway 部署

1. 将本仓库推送到 GitHub。
2. 在 Railway 选择 **New Project → Deploy from GitHub Repo**。
3. 选择本仓库。项目会使用根目录的 `Dockerfile` 构建。
4. Railway 会自动注入 `PORT`，服务会绑定 `0.0.0.0`。
5. 在 Railway 的 **Settings → Networking** 中生成 Public Domain。
6. 打开域名，进入“从飞书同步”，输入 App ID、App Secret 和 Sheet Token。

`railway.json` 已将健康检查配置为 `/healthz`，启动超时设置为 300 秒。

## 飞书同步

服务读取以下 7 张业务子表：

- `零售门店`
- `零售订单明细`
- `零售订单商品`
- `抖音门店记录表`
- `抖音订单`
- `抖音核销`
- `抖音售后`

大表按 600 行分块读取，单次飞书请求超时为 120 秒，并自动重试 3 次。抖音订单数据较多时，一次同步通常需要 1 至 3 分钟。

飞书 App Secret 只随当前同步请求发送，不写入 Git、localStorage、日志或镜像。同步缓存按 App ID 和 Sheet Token 隔离，保存在容器临时目录，服务重启后会清空。

## 可选环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8080` | Railway 自动注入 |
| `HOST` | `0.0.0.0` | 容器监听地址 |
| `FEISHU_SYNC_CACHE_DIR` | `/tmp/feishu-dashboard` | 同步缓存目录 |
| `FEISHU_SYNC_CONCURRENCY` | `2` | 同时执行的飞书同步任务上限 |

## 本地运行

Windows PowerShell：

```powershell
$env:PORT = "8080"
python .\feishu_sync_server.py
```

然后打开：

```text
http://127.0.0.1:8080/
```

## 安全说明

- 不要把 App Secret、生产表格数据或同步缓存提交到 GitHub。
- `.gitignore` 和 `.dockerignore` 已排除本地 Excel、Word、测试脚本、缓存和旧版临时文件。
- 如果 Railway 域名对公网开放，建议在 Railway 或前置网关增加访问控制。
- 飞书应用需具备表格读取权限，并将目标表格授权给该应用。
