"""Local ONNX recognizer. Images live only in RAM; never saved or transmitted."""
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import threading
import unicodedata

import numpy as np

_lock = threading.Lock()
_recognizer = None


def parse_price(text):
    text = unicodedata.normalize('NFKC', text).strip()
    # No digit corrections, no picking one number out of labels or multiple quotes.
    match = re.fullmatch(r'[$¥€£]?\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,12})?)\s*(?:元|USD|CNY)?', text)
    if not match:
        return None
    try:
        value = Decimal(match[1].replace(',', ''))
        return value if 0 < value < Decimal('1e15') else None
    except InvalidOperation:
        return None


def read_price(rgb, confidence=.90):
    import cv2
    global _recognizer
    if rgb.ndim != 3 or min(rgb.shape[:2]) < 8 or rgb.shape[0] > 2000 or rgb.shape[1] > 6000:
        return None
    with _lock:
        if _recognizer is None:
            from rapidocr_onnxruntime.ch_ppocr_rec import TextRecognizer
            model = Path(__file__).resolve().parent / 'ocr_models' / 'ch_PP-OCRv4_rec_infer.onnx'
            if not model.is_file():
                import rapidocr_onnxruntime
                model = Path(rapidocr_onnxruntime.__file__).parent / 'models' / model.name
            _recognizer = TextRecognizer({
                'model_path': str(model), 'use_cuda': False, 'use_dml': False,
                'intra_op_num_threads': 1, 'inter_op_num_threads': 1,
                'rec_batch_num': 1, 'rec_img_shape': [3, 48, 320]})
        original = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        # Normalize dark quote panels without assuming red/green market direction.
        border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
        if np.median(border) < 128:
            gray = 255-gray
        normalized = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        results, _ = _recognizer([original, normalized])
        parsed = [parse_price(text) if score >= confidence else None for text, score in results]
        return float(parsed[0]) if len(parsed) == 2 and parsed[0] is not None and parsed[0] == parsed[1] else None


def image_rgb(image):
    data = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
    return data[:, :image.width()*3].reshape(image.height(), image.width(), 3).copy()
