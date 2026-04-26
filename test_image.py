import os
from pathlib import Path

from src.image_gen import PollinationsImageGenerator


def test_image_generation() -> None:
    print("🎨 Görsel üretici hazırlanıyor...")

    # 1. Klasör kontrolü
    image_dir = Path("assets/images")
    image_dir.mkdir(parents=True, exist_ok=True)

    generator = PollinationsImageGenerator()

    # 2. Test için bir sahne promptu (Claude'un ürettiği tarzda)
    test_prompt = (
        "A dramatic cinematic shot of a futuristic robot coding on a holographic screen, "
        "neon blue lighting, high detail, 8k resolution"
    )
    output_path = str(image_dir / "test_scene_1.jpg")

    print(f"🚀 Üretim başladı: {test_prompt[:50]}...")

    try:
        path = generator.generate_image(test_prompt, output_path)

        if Path(path).exists():
            print(f"✅ Başarılı! Görsel şuraya kaydedildi: {path}")
            print("📏 Lütfen assets/images/test_scene_1.jpg dosyasını açıp dikey (9:16) olup olmadığını kontrol et.")
        else:
            print("❌ Dosya oluşturuldu dendi ama fiziksel olarak bulunamadı.")

    except Exception as e:
        print(f"❌ Hata oluştu: {e}")


if __name__ == "__main__":
    test_image_generation()
