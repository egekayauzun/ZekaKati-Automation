from src.brain import BrainService as Director
from src.utils import load_environment
import json

def test_brain():
    print("🚀 Claude 'Yönetmen' koltuğuna oturuyor, senaryo üretiliyor...")

    load_environment()
    director = Director()
    # Test konusu olarak ilgi çekici bir şey seçelim
    topic = "Yapay zekanın 2026'da yazılımcıları işsiz bırakıp bırakmayacağı"
    
    try:
        script = director.generate_script(topic)
        
        print("\n✅ Senaryo Başarıyla Üretildi!")
        print("-" * 30)
        # Pydantic objesini JSON olarak güzelce yazdıralım
        print(json.dumps(script.model_dump(), indent=4, ensure_ascii=False))
        print("-" * 30)
        
        # Mantıksal Kontrol
        total_scenes = len(script.scenes)
        print(f"🎬 Toplam Sahne Sayısı: {total_scenes}")
        
    except Exception as e:
        print(f"❌ Test sırasında hata oluştu: {e}")

if __name__ == "__main__":
    test_brain()