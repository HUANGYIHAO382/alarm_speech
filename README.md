# alarm_speech

告警信息拉取 + 语音播报助手（Python + Flet 桌面 UI）。

## 功能

- 从监控平台拉取告警历史与活跃告警
- 定时轮询，自动检测新增 / 恢复事件
- 支持 Windows 本地 TTS 与科大讯飞在线 TTS
- 多种播报规则（通用、端口表、自动匹配）

## 环境要求

- Python 3.8+
- Windows（本地 TTS 依赖 Windows SAPI）

## 安装

```bash
# 1. 克隆仓库
git clone https://github.com/HUANGYIHA0382/alarm_speech.git
cd alarm_speech

# 2. 创建虚拟环境 (推荐)
python -m venv alarm_env
alarm_env\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt
```

## 配置

以下文件**不会**随仓库提交，需在本地自行创建：

### 1. 监控平台连接

启动程序后，在界面中填写：

- **服务器 HOST**：监控平台地址
- **访问令牌 Token**：平台颁发的 API Token

### 2. 讯飞 TTS（可选）

若需使用在线语音，在项目根目录创建 `config.local.json`：

```json
{
  "tts": {
    "provider": "xfyun",
    "xfyun": {
      "app_id": "你的APPID",
      "api_key": "你的APIKey",
      "api_secret": "你的APISecret",
      "vcn": "xiaoyan"
    }
  }
}
```

未配置时自动使用 Windows 本地语音。

## 运行

```bash
python flet_demo.py
```

## 项目结构

| 文件 | 说明 |
|------|------|
| `flet_demo.py` | UI 入口 |
| `api_client.py` | 告警 API 网络层 |
| `alarm_processor.py` | 告警清洗、追踪、播报规则 |
| `tts_engine.py` | TTS 引擎调度 |
| `xfyun_tts.py` | 讯飞 WebSocket TTS |
| `tts_config.py` | 配置加载 |

## 许可证

MIT
