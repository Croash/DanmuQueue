# DanmuQueue 使用手册

## 1. 放置配置文件

先把 `config.local.example.json` 复制成 `config.local.json`，放在以下位置之一：

- 开发目录：项目根目录
- macOS 打包版：`~/Library/Application Support/DanmuQueue/config.local.json`
- Windows 打包版：`%APPDATA%\\DanmuQueue\\config.local.json`

## 2. 配置内容

```json
{
  "room": "11113452",
  "keyword": "排",
  "eligibility_mode": "historical",
  "required_guard_level": 3,
  "allow_repeat": false,
  "cookie": ""
}
```

- `room`：直播间号
- `keyword`：入队关键词
- `eligibility_mode`：`historical` / `current` / `all`
- `required_guard_level`：`3=舰长`，`2=提督`，`1=总督`
- `allow_repeat`：是否允许重复排队
- `cookie`：可留空，之后再填

## 3. 启动

```bash
python app.py
```

然后打开：

```text
http://127.0.0.1:8765
```

## 4. 首次使用

1. 先检查配置文件里的直播间号和关键词
2. 需要时再把 Cookie 填进 `config.local.json`
3. 点“连接”
4. 排队结果会自动显示在页面和 overlay

## 5. 导出

- 队列 CSV：`/api/export/queue.csv`
- 队列 TXT：`/api/export/queue.txt`
- 舰队名单 CSV：`/api/export/guards.csv`

TXT 格式示例：

```text
1. 名字 3-2
2. 名字
```
