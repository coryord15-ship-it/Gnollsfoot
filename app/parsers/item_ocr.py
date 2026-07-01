"""
OCR parser for EverQuest item inspect windows.

Usage flow:
  1. Player presses Alt+Print Screen while the EQ item window is focused.
  2. ClipboardWatcher detects the new image.
  3. ocr_image() extracts raw text via tesseract.
  4. parse_item_text() turns that into a structured OcrItem.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Markers that only appear inside EQ item inspect windows
_EQ_MARKERS = ["MAGIC ITEM", "LORE ITEM", "NO DROP", "NODROP",
                "Slot:", "WT:", "Recommended level"]

_SLOT_RE  = re.compile(r'Slot[:\s]+([A-Z][A-Z ,/]+)', re.IGNORECASE)
_AC_RE    = re.compile(r'\bAC[:\s]+(\d+)', re.IGNORECASE)
_HP_RE    = re.compile(r'\bHP[:\s]+\+?(-?\d+)', re.IGNORECASE)
_MANA_RE  = re.compile(r'\bMANA[:\s]+\+?(-?\d+)', re.IGNORECASE)
_END_RE   = re.compile(r'\bEndurance[:\s]+\+?(-?\d+)', re.IGNORECASE)
_STAT_RE  = re.compile(r'\b(STR|STA|AGI|DEX|WIS|INT|CHA)[:\s]+\+?(-?\d+)', re.IGNORECASE)
_SV_RE    = re.compile(r'SV\s+(FIRE|COLD|MAGIC|DISEASE|POISON|CORRUPTION)[:\s]+\+?(-?\d+)', re.IGNORECASE)
_LEVEL_RE = re.compile(r'Recommended level of (\d+)', re.IGNORECASE)
_WT_RE    = re.compile(r'WT[:\s]+([\d.]+)', re.IGNORECASE)
_SIZE_RE  = re.compile(r'Size[:\s]+(\w+)', re.IGNORECASE)
_CLASS_RE = re.compile(r'Class[:\s]+(.+)', re.IGNORECASE)
_RACE_RE  = re.compile(r'Race[:\s]+(.+)', re.IGNORECASE)


@dataclass
class OcrItem:
    item_name: str = ""
    slot: str = ""
    ac: Optional[int] = None
    hp: Optional[int] = None
    mana: Optional[int] = None
    endurance: Optional[int] = None
    stats: dict = field(default_factory=dict)
    saves: dict = field(default_factory=dict)
    level_req: Optional[int] = None
    weight: Optional[float] = None
    size: str = ""
    classes: str = ""
    races: str = ""
    raw_text: str = ""

    def to_description(self) -> str:
        parts = []
        if self.slot:
            parts.append(f"Slot: {self.slot.strip()}")
        if self.ac:
            parts.append(f"AC: {self.ac}")
        stat_parts = []
        for k, v in self.stats.items():
            stat_parts.append(f"{k}: +{v}")
        if self.hp:
            stat_parts.append(f"HP: +{self.hp}")
        if self.mana:
            stat_parts.append(f"MANA: +{self.mana}")
        if self.endurance:
            stat_parts.append(f"END: +{self.endurance}")
        if stat_parts:
            parts.append("  ".join(stat_parts))
        sv_parts = [f"SV {k}: +{v}" for k, v in self.saves.items()]
        if sv_parts:
            parts.append("  ".join(sv_parts))
        if self.level_req:
            parts.append(f"Rec. level {self.level_req}")
        if self.classes and self.classes.strip().upper() not in ("ALL", ""):
            parts.append(f"Class: {self.classes.strip()}")
        return " | ".join(parts) if parts else self.raw_text[:200]


def is_eq_item_window(text: str) -> bool:
    hits = sum(1 for m in _EQ_MARKERS if m.lower() in text.lower())
    return hits >= 2


def parse_item_text(text: str, fallback_name: str = "") -> Optional[OcrItem]:
    if not is_eq_item_window(text):
        log.debug("Clipboard text doesn't match EQ item window pattern")
        return None

    item = OcrItem(raw_text=text)

    m = _SLOT_RE.search(text)
    if m:
        item.slot = m.group(1).strip()
    m = _AC_RE.search(text)
    if m:
        item.ac = int(m.group(1))
    m = _HP_RE.search(text)
    if m:
        item.hp = int(m.group(1))
    m = _MANA_RE.search(text)
    if m:
        item.mana = int(m.group(1))
    m = _END_RE.search(text)
    if m:
        item.endurance = int(m.group(1))
    for m in _STAT_RE.finditer(text):
        item.stats[m.group(1).upper()] = int(m.group(2))
    for m in _SV_RE.finditer(text):
        item.saves[m.group(1).upper()] = int(m.group(2))
    m = _LEVEL_RE.search(text)
    if m:
        item.level_req = int(m.group(1))
    m = _WT_RE.search(text)
    if m:
        item.weight = float(m.group(1))
    m = _SIZE_RE.search(text)
    if m:
        item.size = m.group(1).strip()
    m = _CLASS_RE.search(text)
    if m:
        item.classes = m.group(1).strip()
    m = _RACE_RE.search(text)
    if m:
        item.races = m.group(1).strip()

    # Item name: use first non-marker, non-empty line (usually the title bar)
    marker_words = {"MAGIC", "LORE", "NO", "Slot", "WT", "AC", "HP",
                    "MANA", "Endurance", "Class", "Race", "Size", "SV", "Recommended"}
    for line in text.strip().splitlines():
        line = line.strip()
        first_word = line.split()[0] if line.split() else ""
        if line and first_word not in marker_words:
            item.item_name = line
            break

    if not item.item_name:
        item.item_name = fallback_name

    return item


def ocr_image(image) -> Optional[str]:
    """
    Extract text from a PIL Image.
    Tries Windows.Media.Ocr first (built into Windows 10/11, zero install).
    Falls back to pytesseract if winrt packages aren't available.
    """
    text = _windows_ocr(image)
    if text is not None:
        log.debug("Windows OCR result:\n%s", text[:400])
        return text
    text = _tesseract_ocr(image)
    if text is not None:
        log.debug("Tesseract OCR result:\n%s", text[:400])
    return text


def _windows_ocr(image) -> Optional[str]:
    """Use Windows.Media.Ocr — no external binary, built into Windows 10/11."""
    try:
        import asyncio
        import array as _arr

        # Support both winrt namespace packages and the older winsdk bundle
        try:
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.graphics.imaging import (
                SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode,
            )
            from winrt.windows.storage.streams import DataWriter
        except ImportError:
            from winsdk.windows.media.ocr import OcrEngine          # type: ignore
            from winsdk.windows.graphics.imaging import (            # type: ignore
                SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode,
            )
            from winsdk.windows.storage.streams import DataWriter    # type: ignore

        async def _recognize():
            # PIL RGBA → Windows BGRA8
            img = image.convert("RGBA")
            w, h = img.size
            r, g, b, a = img.split()
            from PIL import Image as _PIL
            bgra = _PIL.merge("RGBA", (b, g, r, a))

            writer = DataWriter()
            writer.write_bytes(_arr.array("B", bgra.tobytes()))
            buf = writer.detach_buffer()

            bmp = SoftwareBitmap.create_copy_from_buffer(
                buf, BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.IGNORE
            )
            engine = OcrEngine.try_create_from_user_profile_languages()
            if not engine:
                return None
            result = await engine.recognize_async(bmp)
            return result.text if result else None

        return asyncio.run(_recognize())
    except Exception as e:
        log.debug("Windows OCR not available: %s", e)
        return None


def _tesseract_ocr(image) -> Optional[str]:
    """Fallback: pytesseract + local tesseract binary."""
    try:
        import pytesseract
        return pytesseract.image_to_string(image, config="--psm 6")
    except ImportError:
        log.debug("pytesseract not installed")
        return None
    except Exception as e:
        log.debug("Tesseract OCR error: %s", e)
        return None
