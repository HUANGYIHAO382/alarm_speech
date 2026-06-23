# alarm_speech

告警信息拉取 + 语音播报助手（Python + Flet 桌面 UI）。

## 功能

- 从监控平台拉取告警历史与活跃告警
- 定时轮询，自动检测新增 / 恢复事件
- 支持 Windows 本地 TTS、MOSS-TTS-Nano 本地模型与科大讯飞在线 TTS
- 多种播报规则（通用、端口表、自动匹配）

## 环境要求

- Python 3.8+
- Windows（本地 TTS 依赖 Windows SAPI）

## 安装

### 一键安装（推荐，clone 后执行）

```powershell
cd alarm_speech
.\setup_all.ps1 -UseMirror    # 国内用户建议加 -UseMirror 加速模型下载
.\run.ps1
```

`setup_all.ps1` 会自动完成：

1. 创建 `alarm_env` 并安装主程序依赖
2. 创建 `moss_env`、克隆 MOSS 源码
3. 从 Hugging Face 预下载约 1GB ONNX 模型
4. 从模板生成 `config.local.json`

可选参数：

| 参数 | 说明 |
|------|------|
| `-UseMirror` | 使用国内 HF 镜像（hf-mirror.com） |
| `-SkipMoss` | 不装 MOSS（仅用 Windows SAPI / 讯飞） |
| `-SkipModels` | 装环境但不预下载模型 |
| `-WithGpu` | 安装 NVIDIA GPU 加速（约 1.5GB 额外下载） |

单独重试模型下载：

```powershell
.\moss_env\Scripts\python.exe download_moss_models.py --mirror
```

### 分步安装

本项目使用**两个独立虚拟环境**，所有依赖只装在项目文件夹内，**不会修改你系统里的 Python 或全局 pip**：

| 虚拟环境 | 用途 | Python 要求 |
|---------|------|------------|
| `alarm_env` | 主程序 Flet 界面 | 3.8+（可用你现有的 3.9） |
| `moss_env` | MOSS 语音模型（可选） | 3.10+（需额外安装 3.12，与 3.9 并存） |

```powershell
cd e:\alarm_speech

# 一键安装主程序环境（推荐）
.\setup.ps1

# 若还需要 MOSS 语音，先安装 Python 3.12（多装一个版本，不覆盖原环境）:
# winget install Python.Python.3.12
# 然后:
.\setup.ps1 -WithMoss
# 或单独: .\setup_moss.ps1

# 启动程序（始终走 alarm_env，不用系统 python）
.\run.ps1
```

手动方式（与上面等价）：

```powershell
python -m venv alarm_env          # 不推荐直接用系统 python，请用 setup_alarm.ps1
alarm_env\Scripts\activate
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

### 3. MOSS-TTS-Nano 本地模型（可选，推荐）

[MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) 是 OpenMOSS 开源的中文语音模型，CPU 即可运行，音质优于系统 SAPI。

**注意：** MOSS 需要 **Python 3.10+**（推荐 3.12），与主程序 `alarm_env` 分开安装；`winget install Python.Python.3.12` 是**多装一个版本**，不会覆盖你现有的 Python 3.9。

```powershell
.\setup_moss.ps1
# 或: .\setup.ps1 -WithMoss

.\alarm_env\Scripts\python.exe test_moss_tts.py
```

安装完成后，在界面「语音引擎」中选择 **MOSS 本地** 即可。

可选配置 `config.local.json`（参考 `config.local.example.json`）：

```json
{
  "tts": {
    "moss": {
      "voice": "Junhao",
      "cpu_threads": 4
    }
  }
}
```

## 运行

```powershell
.\run.ps1
```

或：

```powershell
.\alarm_env\Scripts\python.exe flet_demo.py
```

## 项目结构

详细目录说明见 **[docs/项目结构.md](docs/项目结构.md)**；文档索引见 **[docs/README.md](docs/README.md)**。

| 类别 | 主要文件 |
|------|----------|
| UI | `flet_demo.py` |
| 告警 | `api_client.py`、`alarm_processor.py` |
| 语音 | `tts_engine.py`、`tts_config.py`、`moss_tts.py`、`moss_daemon.py` |
| MOSS/GPU | `moss_cuda_env.py`、`moss_cuda_probe.py`、`fix_moss_gpu.ps1` |
| 日志 | `app_logger.py` → `logs/` |
| 安装 | `setup_all.ps1`（一键）、`setup.ps1`、`setup_moss.ps1`、`download_moss_models.py`、`run.ps1` |
| 文档 | `docs/`（含 UI 方案、MOSS 缓存拼合方案） |

## 许可证

MIT
