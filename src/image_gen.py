from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests

from .logger import LOGGER
from .utils import ensure_directories


class PollinationsImageGenerator:
    """Generate vertical Shorts images (1024x1792) using Pollinations."""

    def __init__(self, base_url: str | None = None) -> None:
        try:
            self.base_url = base_url or "https://image.pollinations.ai/prompt"
            self.images_dir = Path("assets/images")
            ensure_directories([self.images_dir])
        except Exception as e:
            LOGGER.error("Image generator initialization failed: %s", e)
            raise

    def _target_path(self, output_path: str) -> Path:
        candidate = Path(output_path)
        filename = candidate.name or "generated_image.png"
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            filename = f"{Path(filename).stem}.png"
        return self.images_dir / filename

    def generate_image(self, prompt: str, output_path: str) -> Path:
        """Generate image as 9:16 and save under assets/images/."""
        output_file = self._target_path(output_path)
        encoded_prompt = quote(prompt, safe="")
        url = (
            f"{self.base_url}/{encoded_prompt}"
            "?width=1024&height=1792&nologo=true&model=flux"
        )

        try:
            # İleride Flux (Fal.ai) veya DALL-E 3 geçişi için burası güncellenecektir.
            with requests.get(url, stream=True, timeout=90) as response:
                response.raise_for_status()
                with output_file.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

            LOGGER.info("Image generated successfully: %s", output_file)
            return output_file
        except requests.HTTPError as e:
            details = ""
            if e.response is not None:
                details = e.response.text[:500]
            LOGGER.error("Pollinations HTTP error while generating '%s': %s", output_file, details or e)
            raise
        except (requests.RequestException, TimeoutError, OSError) as e:
            LOGGER.error("Image generation connection/file error for '%s': %s", output_file, e)
            raise
        except Exception as e:
            LOGGER.error("Image generation failed for '%s': %s", output_file, e)
            raise


class ImageGenerator(PollinationsImageGenerator):
    """Backward-compatible alias for legacy imports/usages."""
