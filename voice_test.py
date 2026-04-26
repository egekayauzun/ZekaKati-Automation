from src.brain import Director
from src.voice import VoiceGenerator
from src.utils import load_environment
from pathlib import Path
import json

def test_full_flow():
    load_environment()
    # 1. Senaryoyu Al
    director = Director()
    script = director.generate_script("Yapay zekanın geleceği")
    
    # 2. Sese Dönüştür
    voice_gen = VoiceGenerator()
    audio_path = Path("assets/audio/test_voice.mp3")
    
    print("🎙 Seslendiriliyor...")
    path, timestamps = voice_gen.generate_audio(script.voiceover_text, str(audio_path))
    timestamps_path = Path("assets/audio/test_voice_timestamps.json")
    timestamps_path.write_text(json.dumps(timestamps, ensure_ascii=False, indent=2), encoding="utf-8")
    
    print(f"✅ Ses dosyası kaydedildi: {path}")
    print(f"📝 Zaman damgalari kaydedildi: {timestamps_path}")
    print(f"⏱ İlk 3 kelime zamanlaması: {timestamps[:3]}")

if __name__ == "__main__":
    test_full_flow()