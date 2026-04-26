# ZekaKati-Automation

Python tabanli kisa video uretim otomasyonu. Icerik senaryolari icin dil modelleri, seslendirme icin ElevenLabs, gorsel/video materyaller icin Pexels ve kurgu icin MoviePy kullanilir.

## Project Structure

```text
ZekaKati-Automation/
├── src/                # Core source code (video_engine.py, voice.py, etc.)
├── assets/             # Media assets (images, fonts, music, outputs)
├── config/             # Configuration files
├── .env.example        # Environment template (never commit real keys)
├── .gitignore          # Files/folders ignored by Git
├── requirements.txt    # Python dependencies
├── main.py             # Main pipeline entrypoint
├── preview_api.py      # Local API server for UI
├── ui.html             # Local web interface
└── README.md           # Project overview
```

## Quick Start

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python preview_api.py
```

Ardindan arayuzu acmak icin:

```bash
python -m http.server 5500
```

ve `http://localhost:5500/ui.html` adresini ziyaret edin.
