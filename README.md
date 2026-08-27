# DanmuQueue

监听 B 站直播间弹幕，收到包含“排队”的弹幕后，把符合资格的用户写入本地队列。应用自己连接 B 站、自己存储数据、自己导出文件，不需要部署外部后端。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行

### 本地 UI 应用

```bash
python app.py
```

然后打开：

```text
http://127.0.0.1:8765
```

也可以启动后自动打开管理页：

```bash
python app.py --open-browser
```

页面里可以设置：

- 直播间号：支持短号，应用会自动解析真实 room_id
- 入队关键词：支持多个，默认 `排队`，可按换行、逗号、分号或竖线分隔
- 资格来源：`曾经上过舰` / `当前在舰` / `不限制`
- 最低等级：`舰长及以上` / `提督及以上` / `总督`
- 是否允许重复排队
- Cookie：可选，只在本次本地应用运行期间使用，不写入数据库

也可以直接准备本地配置文件 `config.local.json`，应用启动时会自动读取；模板见 `config.local.example.json`。

导出：

- 队列 CSV：`/api/export/queue.csv`
- 队列 TXT：`/api/export/queue.txt`，格式为 `1. 名字 备注`
- 舰队名单 CSV：`/api/export/guards.csv`

直播展示页：

- Overlay：`http://127.0.0.1:8765/overlay`
- 自定义标题：`http://127.0.0.1:8765/overlay?title=街霸排队`

这个页面适合放进 OBS 或直播姬的浏览器源，只显示排队名单，默认格式为 `1. 名字`。overlay 每行右侧可以填写备注/比分，也可以确认后从直播展示页隐藏；后台完整队列、CSV 导出和 TXT 导出仍会保留原始序号与备注。后台的“恢复展示”可以把已隐藏项重新显示到 overlay。

数据会保存在 `danmu_queue.db`。如果你选择“曾经上过舰”，应用会自动记录监听期间看到的 `GUARD_BUY` 上舰事件，也会把弹幕里带舰队等级的用户沉淀进舰队名单。应用启动之前的历史舰队成员，可以在 UI 里手动导入，格式支持：

`曾经上过舰` 表示只要本地舰队名单里有该 UID 的上舰、上提督或上总督记录即可，包含当前在舰和已经不在舰的人；此模式不再使用“最低等级”。`当前在舰` 才会按“最低等级”过滤。弹幕或历史弹幕中如果显示当前不是舰长、但本直播间粉丝牌等级大于等于 21，应用会按“前舰长”沉淀进本地舰队名单。

```csv
UID,昵称,等级
123456,用户名,3
```

等级：`1=总督`，`2=提督`，`3=舰长`。

应用会自动给 B 站 `getDanmuInfo` 请求添加 WBI 签名。如果连接时仍然返回 `-352` 一类错误，可以在 UI 里填入当前浏览器登录 B 站后的 Cookie，或在启动前设置：

```bash
export BILI_COOKIE='你的 B 站 Cookie'
python app.py
```

WebSocket 弹幕线路断开并进入重连时，应用会每秒拉取一次 B 站最近历史弹幕接口，用同一套关键词、资格和去重规则补记排队。这个机制能减少短暂断线造成的漏记，但历史接口只返回有限数量的最近弹幕；如果断线期间弹幕量很大，窗口外的弹幕仍可能无法恢复。

### 桌面应用打包

安装开发依赖并生成图标：

```bash
pip install -r requirements-dev.txt
python scripts/create_icons.py
```

构建当前系统的桌面应用：

```bash
python scripts/build_desktop.py
```

macOS 会生成：

```text
dist/DanmuQueue.app
```

构建 macOS DMG：

```bash
python scripts/build_dmg.py
```

输出文件示例：

```text
dist/DanmuQueue-macOS-arm64.dmg
```

打包应用启动后会自动打开管理页。打包状态下，数据库默认保存在用户数据目录，例如 macOS 的：

```text
~/Library/Application Support/DanmuQueue/danmu_queue.db
```

本地配置文件默认保存在同一目录下的 `config.local.json`。

管理页里的“退出应用”会停止本地服务进程。Windows 版本需要在 Windows 环境中运行同一个构建脚本生成。

如果仓库已经推到 GitHub，可以在 `Actions` 里手动触发 `Windows Package` workflow，执行完后会把 Windows 产物上传成 artifact。

### 命令行版本

```bash
python danmu_queue.py --room 直播间号
```

默认会在当前目录生成 `queue.csv`。每一行是一条排队记录：

```csv
queue_no,queued_at,danmu_time,uid,uname,message
1,2026-08-11T11:30:00+08:00,2026-08-11T11:29:59+08:00,123456,用户名,排队
```

常用参数：

```bash
python danmu_queue.py --room 直播间号 --output data/queue.csv
python danmu_queue.py --room 直播间号 --keyword 上车
python danmu_queue.py --room 直播间号 --allow-repeat
python danmu_queue.py --room 直播间号 --verbose
```

默认会按用户去重：同一个 UID 只记录第一次排队。B 站未登录访问时可能隐藏 UID 和用户名，如果你需要更准确的用户名/UID，可以传入 Cookie：

```bash
export BILI_COOKIE='你的 B 站 Cookie'
python danmu_queue.py --room 直播间号
```

也可以直接传：

```bash
python danmu_queue.py --room 直播间号 --cookie '你的 B 站 Cookie'
```

按 `Ctrl+C` 可以停止监听，已有记录会保留在 CSV 文件中。
