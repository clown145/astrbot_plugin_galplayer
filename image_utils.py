from __future__ import annotations

from pathlib import Path
from typing import Tuple, Union

import cv2
import numpy as np


class ImageProcessingError(RuntimeError):
    """Raised when we fail to extract a meaningful click point from images."""


PathLike = Union[str, Path]


def _load_image(image_path: PathLike) -> np.ndarray:
    path = Path(image_path)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ImageProcessingError(f"无法读取图片: {path}")
    return image


def _resize_annotated_image(original: np.ndarray, annotated: np.ndarray) -> np.ndarray:
    orig_h, orig_w = original.shape[:2]
    ann_h, ann_w = annotated.shape[:2]

    if (ann_h, ann_w) == (orig_h, orig_w):
        return annotated

    scale_w = orig_w / ann_w
    scale_h = orig_h / ann_h

    # 如果宽高缩放比例差异较大，意味着长宽比发生较大变化，提示用户重新上传。
    if abs(scale_w - scale_h) > 0.2:
        raise ImageProcessingError("标注图片的长宽比变化过大，请不要裁剪图片，仅在原图上涂鸦后重新发送。")

    interpolation = cv2.INTER_LINEAR if scale_w > 1.0 or scale_h > 1.0 else cv2.INTER_AREA
    resized = cv2.resize(annotated, (orig_w, orig_h), interpolation=interpolation)
    return resized


def extract_click_point(
    original_path: PathLike, annotated_path: PathLike
) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Extract the centroid of the scribbled area and return (point, (width, height))."""
    original = _load_image(original_path)
    annotated = _load_image(annotated_path)

    annotated = _resize_annotated_image(original, annotated)

    # 计算差异图
    diff = cv2.absdiff(original, annotated)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    diff_blur = cv2.GaussianBlur(diff_gray, (5, 5), 0)

    # 使用 OTSU 自动阈值过滤噪声
    _, thresh = cv2.threshold(diff_blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    kernel = np.ones((3, 3), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=1)

    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ImageProcessingError("未检测到有效的标注区域，请使用明显的颜色或更粗的线条。")

    orig_h, orig_w = original.shape[:2]
    min_area = max(60, int(0.0005 * orig_w * orig_h))
    candidates = [cnt for cnt in contours if cv2.contourArea(cnt) >= min_area]
    if not candidates:
        raise ImageProcessingError("标注区域太小，无法识别，请尝试更明显的标记。")

    target = max(candidates, key=cv2.contourArea)
    moments = cv2.moments(target)

    if moments["m00"] == 0:
        x, y, w, h = cv2.boundingRect(target)
        centroid = (x + w // 2, y + h // 2)
    else:
        centroid = (int(moments["m10"] / moments["m00"]), int(moments["m01"] / moments["m00"]))

    height, width = original.shape[:2]
    return centroid, (width, height)
