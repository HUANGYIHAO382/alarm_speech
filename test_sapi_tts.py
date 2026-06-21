"""
Windows SAPI 语音播报 — 独立测试脚本
=====================================
不依赖 Flet / 告警 API，专门用来试听 SAPI 播报效果。

用法:
  python test_sapi_tts.py                          # 交互模式
  python test_sapi_tts.py --list-voices            # 列出本机语音
  python test_sapi_tts.py -t "你好，测试播报" -r -4   # 指定文字和语速
  python test_sapi_tts.py --samples                # 朗读预设样例（含告警话术）
  python test_sapi_tts.py --samples --polish       # 样例经 polish 后再播

语速 -10(最慢) ~ 10(最快)，默认 -4。
"""
from __future__ import annotations

import argparse
import sys

def _load_samples():
    """与主程序 alarm_processor 共用同一套话术生成。"""
    try:
        from alarm_processor import build_port_table_speech_text, polish_speech_for_tts
        return [
            ("纯中文", "你好，这是 Windows SAPI 中文语音测试。"),
            ("告警-英文状态(旧)", "B05柜的NE20E-1 端口 up"),
            ("告警-优化后", build_port_table_speech_text("B05_NE20E-1", "up")),
            ("告警-故障", build_port_table_speech_text("B05_NE20E-1", "down")),
            ("通用恢复", polish_speech_for_tts("恢复，B05_NE20E-1，已恢复正常")),
        ]
    except ImportError:
        return [
            ("纯中文", "你好，这是 Windows SAPI 中文语音测试。"),
            ("告警-优化后", "B05柜，NE20E杠1，端口，联通"),
            ("告警-故障", "B05柜，NE20E杠1，端口，中断"),
        ]


SAMPLES = _load_samples()


def list_voices(speaker) -> list[tuple[int, str]]:
    voices = speaker.GetVoices()
    result = []
    for i in range(voices.Count):
        v = voices.Item(i)
        result.append((i, v.GetDescription()))
    return result


def select_chinese_voice(speaker) -> str:
    """优先选中文语音，返回当前语音描述。"""
    voices = speaker.GetVoices()
    keywords = ("huihui", "kangkang", "yaoyao", "chinese", "zh-cn", "0804", "中文")
    for i in range(voices.Count):
        desc = voices.Item(i).GetDescription()
        if any(k in desc.lower() for k in keywords):
            speaker.Voice = voices.Item(i)
            return desc
    return speaker.Voice.GetDescription()


def create_speaker():
    try:
        import pythoncom
        import win32com.client
    except ImportError as err:
        print("❌ 需要 pywin32: pip install pywin32")
        raise SystemExit(1) from err

    pythoncom.CoInitialize()
    return win32com.client.Dispatch("SAPI.SpVoice")


def speak(speaker, text: str, rate: int) -> None:
    speaker.Rate = max(-10, min(10, rate))
    print(f"▶ 语速={speaker.Rate} | 语音={speaker.Voice.GetDescription()}")
    print(f"▶ 播报: {text}")
    speaker.Speak(text)
    print("✅ 播报完成\n")


def run_interactive(speaker, rate: int, polish: bool) -> None:
    voice_name = speaker.Voice.GetDescription()
    print("=" * 60)
    print("SAPI 交互测试 (输入 q 退出, samples 播预设样例)")
    print(f"当前语音: {voice_name}")
    print(f"当前语速: {rate}  (可用 rate -6 调整)")
    print("=" * 60)

    while True:
        try:
            line = input("\n请输入要播报的文字> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break
        if line.lower() == "voices":
            for idx, desc in list_voices(speaker):
                mark = " ← 当前" if desc == voice_name else ""
                print(f"  [{idx}] {desc}{mark}")
            continue
        if line.lower().startswith("rate "):
            try:
                rate = int(line.split()[1])
                rate = max(-10, min(10, rate))
                print(f"语速已设为 {rate}")
            except (IndexError, ValueError):
                print("用法: rate -4")
            continue
        if line.lower() == "samples":
            for label, text in SAMPLES:
                t = _maybe_polish(text, polish)
                print(f"\n--- 样例: {label} ---")
                speak(speaker, t, rate)
            continue

        text = _maybe_polish(line, polish)
        speak(speaker, text, rate)


def _maybe_polish(text: str, polish: bool) -> str:
    if not polish:
        return text
    try:
        from alarm_processor import polish_speech_for_tts
        return polish_speech_for_tts(text)
    except ImportError:
        return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows SAPI 语音播报测试")
    parser.add_argument("-t", "--text", help="要播报的文字")
    parser.add_argument("-r", "--rate", type=int, default=-4, help="语速 -10~10，默认 -4")
    parser.add_argument("--list-voices", action="store_true", help="列出本机安装的 SAPI 语音")
    parser.add_argument("--voice-index", type=int, help="使用指定序号的语音 (配合 --list-voices)")
    parser.add_argument("--samples", action="store_true", help="朗读内置样例")
    parser.add_argument("--polish", action="store_true", help="经 alarm_processor 优化后再播")
    args = parser.parse_args()

    speaker = create_speaker()

    if args.list_voices:
        print("本机 SAPI 语音列表:")
        for idx, desc in list_voices(speaker):
            print(f"  [{idx}] {desc}")
        return

    if args.voice_index is not None:
        voices = speaker.GetVoices()
        if args.voice_index < 0 or args.voice_index >= voices.Count:
            print(f"❌ 无效序号，范围 0 ~ {voices.Count - 1}")
            sys.exit(1)
        speaker.Voice = voices.Item(args.voice_index)
    else:
        chosen = select_chinese_voice(speaker)
        print(f"已选用语音: {chosen}")

    if args.samples:
        for label, text in SAMPLES:
            t = _maybe_polish(text, args.polish)
            print(f"\n--- 样例: {label} ---")
            speak(speaker, t, args.rate)
        return

    if args.text:
        text = _maybe_polish(args.text, args.polish)
        speak(speaker, text, args.rate)
        return

    run_interactive(speaker, args.rate, args.polish)


if __name__ == "__main__":
    main()
