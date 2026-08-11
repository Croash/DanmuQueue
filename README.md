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

页面里可以设置：

- 直播间号：支持短号，应用会自动解析真实 room_id
- 关键词：默认 `排队`
- 资格来源：`曾经上过舰` / `当前在舰` / `不限制`
- 最低等级：`舰长及以上` / `提督及以上` / `总督`
- 是否允许重复排队
- Cookie：可选，只在本次本地应用运行期间使用，不写入数据库

导出：

- 队列 CSV：`/api/export/queue.csv`
- 舰队名单 CSV：`/api/export/guards.csv`

数据会保存在 `danmu_queue.db`。如果你选择“曾经上过舰”，应用会自动记录监听期间看到的 `GUARD_BUY` 上舰事件，也会把弹幕里带舰队等级的用户沉淀进舰队名单。应用启动之前的历史舰队成员，可以在 UI 里手动导入，格式支持：

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
