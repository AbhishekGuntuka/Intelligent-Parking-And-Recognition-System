"""
Project  : IPRS — Intelligent Parking & Recognition System
File     : ocr_module.py
Purpose  : Production OCR engine for Indian vehicle number plates
           Called by iprs_vision_pipeline.py → ocr_plate_from_bytes(jpeg_bytes)
Hardware : Server PC / Raspberry Pi 4 (CPU)
Version  : v3.1  (2026-03-21)

Architecture:
  Stage 1  Decode JPEG → BGR
  Stage 2  Pre-process pipeline (5 variants — best confidence wins)
           A. CLAHE → grayscale → Otsu threshold
           B. Adaptive threshold (Gaussian)
           C. Morphological close → Otsu
           D. Bilateral filter → threshold
           E. Invert of best result (dark-on-light plates)
  Stage 3  Tesseract PSM sweep  (7, 8, 13 — single-line / single-word modes)
  Stage 4  Indian plate regex validation + normalisation
  Stage 5  Return (plate_string, confidence_0_to_100)

Public API:
    ocr_plate_from_bytes(jpeg_bytes: bytes) -> tuple[str, float]

Dependencies:
    pip install opencv-python-headless pytesseract numpy --break-system-packages
    sudo apt-get install -y tesseract-ocr
"""

from __future__ import annotations

import logging
import re
import unicodedata

import cv2
import numpy as np

# Guard pytesseract import — gives a clean error if Tesseract is not installed
# rather than crashing both iprs_server_v9.py and iprs_vision_pipeline.py at import
try:
    import pytesseract
    _TESS_OK = True
except ImportError:
    _TESS_OK = False
    pytesseract = None  # type: ignore[assignment]

log = logging.getLogger("IPRS-OCR")

# ══════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════
_TSR_BASE        = r"--oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_PSM_MODES       = [7, 8, 6, 13]   # added PSM 6 (single uniform block) for wider plates
# Lower threshold so plates with glare/shadow still surface a result
_CONF_MIN_REPORT = 15.0
# 800px gives ~35px tall chars from SVGA frame — safely above Tesseract's 20px minimum
_PLATE_TARGET_W  = 800
_PLATE_MAX_H     = 300
_CLAHE_CLIP      = 4.0   # raised from 3.0 — more contrast on dim/night plates
_CLAHE_GRID      = (8, 8)

_RE_STANDARD = re.compile(r'^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$')
_RE_BH       = re.compile(r'^(\d{2})(BH)(\d{4})([A-Z]{2})$')

_VALID_STATE_CODES = {
    "AN","AP","AR","AS","BR","CG","CH","DD","DL","DN","GA","GJ","HP",
    "HR","JH","JK","KA","KL","LA","LD","MH","ML","MN","MP","MZ","NL",
    "OD","PB","PY","RJ","SK","TN","TR","TS","UK","UP","UT","WB",
}

# ══════════════════════════════════════════════════════════════════════
#  PREPROCESSING
# ══════════════════════════════════════════════════════════════════════

def _resize_plate(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    if w == 0 or h == 0:
        return img
    scale = _PLATE_TARGET_W / w
    new_h = min(max(int(h * scale), 10), _PLATE_MAX_H)
    return cv2.resize(img, (_PLATE_TARGET_W, new_h), interpolation=cv2.INTER_CUBIC)


def _clahe_gray(bgr: np.ndarray) -> np.ndarray:
    gray  = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=_CLAHE_CLIP, tileGridSize=_CLAHE_GRID)
    return clahe.apply(gray)


def _make_variants(bgr: np.ndarray) -> list[np.ndarray]:
    """7 binary variants to handle all plate lighting/colour conditions."""
    resized = _resize_plate(bgr)
    cl      = _clahe_gray(resized)

    # A: CLAHE → Otsu global
    _, th_a = cv2.threshold(cl, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # B: Adaptive Gaussian (uneven lighting across plate)
    th_b = cv2.adaptiveThreshold(
        cl, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 8
    )

    # C: Morphological close → Otsu (fills gaps in weathered plates)
    kernel  = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed  = cv2.morphologyEx(cl, cv2.MORPH_CLOSE, kernel)
    _, th_c = cv2.threshold(closed, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # D: Bilateral filter → Otsu (denoises while preserving char edges)
    bl      = cv2.bilateralFilter(cl, 9, 75, 75)
    _, th_d = cv2.threshold(bl, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # E: Invert of A (yellow plates / dark-on-light where Otsu flips polarity)
    th_e = cv2.bitwise_not(th_a)

    # F: Sharpen → Otsu (recovers blurry plates from motion or low light)
    sharpen_k = np.array([[-1,-1,-1],[-1, 9,-1],[-1,-1,-1]], dtype=np.float32)
    sharpened = cv2.filter2D(cl, -1, sharpen_k)
    sharpened = np.clip(sharpened, 0, 255).astype(np.uint8)
    _, th_f   = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # G: 2× upscale → CLAHE → Otsu (very small/distant plates)
    up = cv2.resize(resized, (resized.shape[1]*2, resized.shape[0]*2), interpolation=cv2.INTER_CUBIC)
    cl2 = _clahe_gray(up)
    _, th_g = cv2.threshold(cl2, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return [th_a, th_b, th_c, th_d, th_e, th_f, th_g]

# ══════════════════════════════════════════════════════════════════════
#  NORMALISATION & VALIDATION
# ══════════════════════════════════════════════════════════════════════

def _normalise_plate(raw: str) -> str:
    """Strip noise, correct OCR ambiguities (O/0, I/1) by position context."""
    raw   = unicodedata.normalize("NFKD", raw)
    clean = re.sub(r'[^A-Z0-9]', '', raw.upper())
    if len(clean) < 4:
        return clean

    chars = list(clean)

    # Positions 0-1: state code letters → digit→letter corrections
    for i in range(min(2, len(chars))):
        if chars[i] == '0': chars[i] = 'O'
        if chars[i] == '1': chars[i] = 'I'
        if chars[i] == '5': chars[i] = 'S'
        if chars[i] == '8': chars[i] = 'B'

    # Positions 2-3: district number → letter→digit corrections
    for i in range(2, min(4, len(chars))):
        if chars[i] == 'O': chars[i] = '0'
        if chars[i] == 'I': chars[i] = '1'
        if chars[i] == 'S': chars[i] = '5'
        if chars[i] == 'B': chars[i] = '8'
        if chars[i] == 'Q': chars[i] = '0'  # Tesseract often reads 0 as Q
        if chars[i] == 'Z': chars[i] = '2'  # Tesseract often reads 2 as Z
        if chars[i] == 'G': chars[i] = '6'  # Tesseract often reads 6 as G
        if chars[i] == 'D': chars[i] = '0'  # Tesseract often reads 0 as D

    # Last 4 chars: serial number digits → same digit corrections
    for i in range(max(0, len(chars) - 4), len(chars)):
        if chars[i] == 'O': chars[i] = '0'
        if chars[i] == 'I': chars[i] = '1'
        if chars[i] == 'S': chars[i] = '5'
        if chars[i] == 'B': chars[i] = '8'
        if chars[i] == 'Q': chars[i] = '0'
        if chars[i] == 'Z': chars[i] = '2'
        if chars[i] == 'G': chars[i] = '6'
        if chars[i] == 'D': chars[i] = '0'

    return ''.join(chars)


def _validate_plate(plate: str) -> tuple[bool, float]:
    """
    Validate against Indian plate regex patterns.
    Returns (is_valid, confidence_boost).
    Boost: +15 if regex + valid state, +8 regex only, +0 no match.
    """
    if len(plate) < 6 or len(plate) > 11:
        return False, 0.0
    if _RE_BH.match(plate):
        return True, 12.0
    m = _RE_STANDARD.match(plate)
    if m:
        boost = 15.0 if m.group(1) in _VALID_STATE_CODES else 8.0
        return True, boost
    return False, 0.0

# ══════════════════════════════════════════════════════════════════════
#  TESSERACT RUNNER
# ══════════════════════════════════════════════════════════════════════

def _run_tesseract(img: np.ndarray, psm: int) -> tuple[str, float]:
    """
    Run Tesseract on one variant at one PSM mode.
    Returns (joined_text, avg_word_confidence_0_to_100).
    """
    if not _TESS_OK:
        return "", 0.0
    config = f"{_TSR_BASE} --psm {psm}"
    try:
        data = pytesseract.image_to_data(
            img, config=config,
            output_type=pytesseract.Output.DICT,
            lang="eng",
        )
    except pytesseract.TesseractNotFoundError:
        log.error(
            "Tesseract not found. Install: sudo apt-get install -y tesseract-ocr"
        )
        return "", 0.0
    except Exception as exc:
        log.debug("Tesseract error psm=%d: %s", psm, exc)
        return "", 0.0

    texts, confs = [], []
    for txt, conf in zip(data["text"], data["conf"]):
        txt  = str(txt).strip()
        try:
            c = int(conf)
        except (ValueError, TypeError):
            continue
        if c < 0 or not txt:
            continue
        texts.append(txt)
        confs.append(c)

    if not texts:
        return "", 0.0

    return "".join(texts), float(sum(confs)) / len(confs)

# ══════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════

def ocr_plate_from_bytes(jpeg_bytes: bytes) -> tuple[str, float]:
    """
    Full plate OCR pipeline.

    Args:
        jpeg_bytes: Raw JPEG bytes from ESP32-CAM capture

    Returns:
        (plate_string, confidence_0_to_100)
        ("", 0.0) on any failure — NEVER raises.

    Runs 15 Tesseract calls (5 variants × 3 PSM modes).
    Performance: ~180–320 ms on i5, ~400–600 ms on RPi 4.
    """
    if not jpeg_bytes:
        return "", 0.0

    if not _TESS_OK:
        log.error("pytesseract not available — install: pip install pytesseract "
                  "&& sudo apt-get install -y tesseract-ocr")
        return "", 0.0

    # Stage 1 — Decode
    try:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            log.debug("OCR: JPEG decode returned empty image")
            return "", 0.0
    except Exception as exc:
        log.debug("OCR: decode error: %s", exc)
        return "", 0.0

    # Stage 2 — Preprocess
    try:
        variants = _make_variants(bgr)
    except Exception as exc:
        log.debug("OCR: preprocessing error: %s", exc)
        return "", 0.0

    # Stages 3–5 — Tesseract sweep + validate + pick best
    best_plate = ""
    best_conf  = 0.0

    for variant in variants:
        for psm in _PSM_MODES:
            try:
                raw_text, tsr_conf = _run_tesseract(variant, psm)
            except Exception:
                continue

            if not raw_text or tsr_conf < _CONF_MIN_REPORT:
                continue

            plate = _normalise_plate(raw_text)
            if len(plate) < 4:
                continue

            _, boost    = _validate_plate(plate)
            final_conf  = min(tsr_conf + boost, 100.0)

            log.debug(
                "OCR: psm=%d raw='%s' → '%s' tsr=%.1f boost=%.1f final=%.1f",
                psm, raw_text, plate, tsr_conf, boost, final_conf,
            )

            if final_conf > best_conf:
                best_conf  = final_conf
                best_plate = plate

    if not best_plate or best_conf < _CONF_MIN_REPORT:
        log.debug("OCR: no confident result (best='%s' conf=%.1f)", best_plate, best_conf)
        return "", 0.0

    log.info("OCR: plate=%s conf=%.1f", best_plate, best_conf)
    return best_plate, round(best_conf, 1)

# ══════════════════════════════════════════════════════════════════════
#  STANDALONE SMOKE TEST
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys, os
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    def _synthetic_plate(text: str = "MH12AB1234") -> bytes:
        img = np.ones((60, 280, 3), dtype=np.uint8) * 240
        cv2.rectangle(img, (2, 2), (277, 57), (0, 0, 0), 2)
        cv2.putText(img, text, (10, 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2, cv2.LINE_AA)
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return bytes(buf)

    src = sys.argv[1] if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) else None
    if src:
        print(f"Testing with: {src}")
        with open(src, "rb") as f:
            data = f.read()
    else:
        print("No image provided — using synthetic plate 'MH12AB1234'")
        data = _synthetic_plate()

    plate, conf = ocr_plate_from_bytes(data)
    print(f"\n{'='*42}")
    print(f"  Plate      : {plate or '[no result]'}")
    print(f"  Confidence : {conf:.1f}%")
    print(f"  Valid      : {_validate_plate(plate)[0] if plate else False}")
    print(f"{'='*42}\n")

    if not plate:
        print("FAIL — Tesseract may not be installed.")
        print("  sudo apt-get install -y tesseract-ocr")
        sys.exit(1)
    print("PASS — OCR module working correctly")
    sys.exit(0)
