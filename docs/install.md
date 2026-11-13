# 安装到 DSH

插件包含宿主侧 `lib/`、Python 后端 `core/` 和 Cordis 清单，无需自行编译。后端只依赖 Python 3.10+ 标准库；DSH 安装包的 Python 运行时可供插件使用，也可在设置页指定解释器。

## GitHub 来源

仓库公开后，在终端执行：

```bash
dsh plugin --profile desktop add github:zeros-Ctrl/dsh-soulcore
```

重新启动 DSH，让 desktop profile 加载插件。

## 下载后本地安装

1. 在 GitHub 下载仓库 ZIP 或发布包并解压。
2. 找到同时包含 `package.json`、`cordis.patch.yml`、`lib/`、`core/` 的目录。
3. 用该目录的完整路径安装：

```powershell
dsh plugin --profile desktop add "link:C:\Plugins\dsh-soulcore"
```

macOS / Linux 示例：

```bash
dsh plugin --profile desktop add "link:/home/user/plugins/dsh-soulcore"
```

本地 link 安装直接使用这个目录，后续保留它即可。更新目录内容后重新启动 DSH。

## 首次使用

打开「设置 → 人格与记忆」：

- 修改名字、人设卡和称呼。人设写你希望长期保持的性格；本次语气可直接在聊天里表达。
- DSH 对话使用 DSH 当前选择的模型。额外配置插件 DeepSeek API Key 后，可使用模型提炼与 QQ 回复；未配置时记忆用规则兜底。
- 接 QQ 时填写你的 QQ 号，并按 [QQ 接入](qq.md) 连接 NapCat。
- 主动聊天默认关闭；开启后默认生成草稿，可在设置页查看效果。

安装后试着说“记住，我喜欢简短直接的回答”，再换一个会话询问偏好。也可在下载目录运行离线演示：

```bash
python -I core/run.py --mock showcase
```

记忆和人设默认写到 `~/.dsh/soulcore/`，不写到插件安装目录。版本更新会沿用这份数据。

## 排查

- 设置页没有「人格与记忆」：确认安装到 `desktop` profile，目录内清单完整，并重新启动 DSH。
- 后端未就绪：在设置页查看 Python 路径和错误；使用 Python 3.10+。
- QQ 收不到回复：检查插件 API Key、NapCat 地址/Token、机器人登录状态及白名单。

[聊天控制](chat.md) · [配置项](config.md) · [README](../README.md)
