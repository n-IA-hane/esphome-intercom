"""Bounded RFC 6236 image preferences, independent of device and codec owners."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re

MAX_ATTRIBUTE_BYTES = 2048
MAX_SETS = 8
MAX_VALUES = 16
MAX_DIMENSION = 999999
MAX_JPEG_SIDE = 1280
MAX_JPEG_PIXELS = 1280 * 800


@dataclass(frozen=True, slots=True)
class ImageRange:
    minimum: int
    maximum: int
    step: int = 1
    values: tuple[int, ...] = ()

    def allows(self, value: int) -> bool:
        if self.values:
            return value in self.values
        return self.minimum <= value <= self.maximum and (value - self.minimum) % self.step == 0

    def wire(self) -> str:
        if self.values:
            return str(self.values[0]) if len(self.values) == 1 else "[" + ",".join(map(str, self.values)) + "]"
        if self.minimum == self.maximum:
            return str(self.minimum)
        middle = "" if self.step == 1 else f"{self.step}:"
        return f"[{self.minimum}:{middle}{self.maximum}]"


@dataclass(frozen=True, slots=True)
class ImageSet:
    x: ImageRange
    y: ImageRange
    q: int = 50  # RFC preference hundredths, unrelated to JPEG quality.
    par: tuple[Fraction, Fraction] | None = None

    def allows(self, width: int, height: int) -> bool:
        return bool(self.x.allows(width) and self.y.allows(height) and
                    (self.par is None or (
                        self.par[0].numerator * height <= width * self.par[0].denominator and
                        width * self.par[1].denominator <= self.par[1].numerator * height)))

    def wire(self) -> str:
        value = f"[x={self.x.wire()},y={self.y.wire()},sar=1.0"
        if self.par is not None:
            value += f",par=[{float(self.par[0]):.4f}-{float(self.par[1]):.4f}]"
        return value + f",q={self.q / 100:.2f}]"


@dataclass(frozen=True, slots=True)
class ImageConstraints:
    sets: tuple[ImageSet, ...] = ()
    wildcard: bool = False


@dataclass(frozen=True, slots=True)
class ImageAttributes:
    send: ImageConstraints | None = None
    recv: ImageConstraints | None = None


ANY_IMAGE_ATTRIBUTES = ImageAttributes(ImageConstraints(wildcard=True), ImageConstraints(wildcard=True))


class ImageAttributeFallbackRequired(ValueError):
    """An optional, valid answer requires one legacy O/A retry (RFC 6236)."""

    def __init__(self, payload_type: int):
        self.payload_type = payload_type
        super().__init__(f"JPEG image attributes have no usable size for payload {payload_type}")


def _split(value: str, delimiter: str) -> list[str]:
    parts, start, depth = [], 0, 0
    for index, char in enumerate(value):
        if char == "[":
            depth += 1
            if depth > 2:
                raise ValueError("nested image range")
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced image range")
        elif char == delimiter and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    if depth:
        raise ValueError("unbalanced image range")
    return [*parts, value[start:]]


def _integer(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,5}", value):
        raise ValueError("invalid image dimension")
    return int(value)


def _range(value: str) -> ImageRange:
    if not value.startswith("["):
        number = _integer(value)
        return ImageRange(number, number)
    if not value.endswith("]"):
        raise ValueError("invalid image range")
    inner = value[1:-1]
    if ":" in inner:
        numbers = [_integer(item) for item in inner.split(":")]
        if len(numbers) not in {2, 3} or numbers[0] >= numbers[-1]:
            raise ValueError("invalid image interval")
        return ImageRange(numbers[0], numbers[-1], numbers[1] if len(numbers) == 3 else 1)
    values = tuple(_integer(item) for item in inner.split(","))
    if not 2 <= len(values) <= MAX_VALUES:
        raise ValueError("invalid discrete image range")
    return ImageRange(min(values), max(values), values=values)


def _ratio(value: str) -> Fraction:
    if not re.fullmatch(r"(?:0\.[1-9][0-9]{0,3}|[1-9]\.[0-9]{1,4})", value):
        raise ValueError("invalid image aspect ratio")
    return Fraction(value)


def _set(value: str) -> ImageSet | None:
    fields = {}
    for field in _split(value[1:-1], ","):
        key, separator, raw = field.partition("=")
        if not separator or key in fields:
            raise ValueError("duplicate or missing image parameter")
        fields[key] = raw
    x, y = _range(fields.pop("x")), _range(fields.pop("y"))
    quality = fields.pop("q", "0.5")
    if not re.fullmatch(r"(?:0\.[0-9]{1,2}|1\.0{1,2})", quality):
        raise ValueError("invalid image preference")
    sar = fields.pop("sar", "1.0")
    if sar.startswith("[") and sar.endswith("]"):
        inner = sar[1:-1]
        if "-" in inner:
            low, high = map(_ratio, inner.split("-"))
            square = low < high and low <= 1 <= high
        else:
            values = inner.split(",")
            if len(values) > MAX_VALUES:
                raise ValueError("too many sample aspect values")
            square = 1 in tuple(_ratio(item) for item in values)
    else:
        square = _ratio(sar) == 1
    par = fields.pop("par", None)
    if par is not None:
        if not par.startswith("[") or not par.endswith("]"):
            return None  # Optional unsupported form, never reject audio/SDP.
        low, high = map(_ratio, par[1:-1].split("-"))
        if low >= high:
            raise ValueError("invalid picture aspect interval")
        par = low, high
    # Unknown extension keys are ignored and never emitted in an answer.
    return ImageSet(x, y, int(Fraction(quality) * 100), par) if square else None


def parse_imageattr(value: str) -> tuple[int | None, ImageAttributes] | None:
    """Parse an optional media attribute; unsupported input is ignored locally."""
    try:
        if len(value) > MAX_ATTRIBUTE_BYTES or any(ord(c) < 32 and c != "\t" for c in value):
            return None
        match = re.fullmatch(r"(\*|[0-9]{1,3})[ \t]+(.+)", value.strip())
        if match is None:
            return None
        payload = None if match[1] == "*" else int(match[1])
        if payload is not None and payload > 127:
            return None
        tokens = [token for token in _split(match[2].replace("\t", " "), " ") if token]
        directions = {}
        index = 0
        while index < len(tokens):
            direction = tokens[index]
            if direction not in {"send", "recv"} or direction in directions:
                return None
            index += 1
            if index == len(tokens):
                return None
            if tokens[index] == "*":
                directions[direction] = ImageConstraints(wildcard=True)
                index += 1
                continue
            sets = []
            count = 0
            while index < len(tokens) and tokens[index].startswith("["):
                count += 1
                if count > MAX_SETS or not tokens[index].endswith("]"):
                    return None
                image_set = _set(tokens[index])
                if image_set is not None:
                    sets.append(image_set)
                index += 1
            if sets:
                directions[direction] = ImageConstraints(tuple(sets))
            elif count == 0:
                return None
        return (payload, ImageAttributes(**directions)) if directions else None
    except (ValueError, KeyError, ZeroDivisionError):
        return None


def serialize_imageattr(payload_type: int, attributes: ImageAttributes) -> str:
    parts = [str(payload_type)]
    for name in ("send", "recv"):
        direction = getattr(attributes, name)
        if direction is not None:
            parts.extend([name, "*" if direction.wildcard else " ".join(item.wire() for item in direction.sets)])
    return "imageattr:" + " ".join(parts)


def merge_image_attributes(first: ImageAttributes | None, second: ImageAttributes) -> ImageAttributes:
    """Retain split send/recv lines and bounded alternatives in wire order."""
    if first is None:
        return second
    def merge(a, b):
        if a is None or a.wildcard:
            return b or a
        if b is None or b.wildcard:
            return a
        return ImageConstraints(tuple(dict.fromkeys((*a.sets, *b.sets)))[:MAX_SETS])
    return ImageAttributes(merge(first.send, second.send), merge(first.recv, second.recv))


def constraints_payload(attributes: ImageAttributes | None) -> dict | None:
    """Project immutable SDP preferences; the sender chooses using actual frames."""
    if attributes is None:
        return None
    result = {}
    for name in ("send", "recv"):
        constraints = getattr(attributes, name)
        if constraints is None:
            continue
        result[name] = {"wildcard": constraints.wildcard, "sets": [
            {"x": {"min": s.x.minimum, "max": s.x.maximum, "step": s.x.step, "values": list(s.x.values)},
             "y": {"min": s.y.minimum, "max": s.y.maximum, "step": s.y.step, "values": list(s.y.values)},
             "q": s.q, "par": None if s.par is None else [float(v) for v in s.par]}
            for s in constraints.sets]}
    return result


@dataclass(frozen=True, slots=True)
class JpegImagePlan:
    width: int
    height: int
    content_width: int
    content_height: int
    offset_x: float
    offset_y: float


def plan_jpeg_image(constraints: ImageConstraints | None, width: int, height: int) -> JpegImagePlan | None:
    if constraints is None or constraints.wildcard or width <= 0 or height <= 0:
        return None
    for item in sorted(constraints.sets, key=lambda s: -s.q):
        best = None
        best_score = None
        max_width = min(item.x.maximum, MAX_JPEG_SIDE, ((width + 7) // 8) * 8)
        max_height = min(item.y.maximum, MAX_JPEG_SIDE, ((height + 7) // 8) * 8)
        for canvas_width in range(8, max_width + 1, 8):
            if not item.x.allows(canvas_width):
                continue
            for canvas_height in range(8, max_height + 1, 8):
                if canvas_width * canvas_height > MAX_JPEG_PIXELS or not item.allows(canvas_width, canvas_height):
                    continue
                scale = min(1.0, canvas_width / width, canvas_height / height)
                content_width = max(1, int(width * scale + 1e-9))
                content_height = max(1, int(height * scale + 1e-9))
                area = content_width * content_height
                score = area, -(canvas_width * canvas_height - area)
                if best_score is None or score > best_score:
                    best_score = score
                    best = JpegImagePlan(canvas_width, canvas_height, content_width, content_height,
                                         (canvas_width - content_width) / 2, (canvas_height - content_height) / 2)
        if best is not None:
            return best
    return None


def constraints_usable(constraints: ImageConstraints | None) -> bool:
    if constraints is None or constraints.wildcard:
        return True
    for item in constraints.sets:
        for width in range(8, min(item.x.maximum, MAX_JPEG_SIDE) + 1, 8):
            if not item.x.allows(width):
                continue
            for height in range(8, min(item.y.maximum, MAX_JPEG_SIDE) + 1, 8):
                if width * height <= MAX_JPEG_PIXELS and item.allows(width, height):
                    return True
    return False


def _intersection_range(left: ImageRange, right: ImageRange) -> ImageRange | None:
    values = tuple(value for value in range(8, min(left.maximum, right.maximum, MAX_JPEG_SIDE) + 1, 8)
                   if left.allows(value) and right.allows(value))
    if not values:
        return None
    if len(values) == 1:
        return ImageRange(values[0], values[0])
    step = values[1] - values[0]
    if all(b - a == step for a, b in zip(values, values[1:])):
        return ImageRange(values[0], values[-1], step)
    return ImageRange(values[0], values[-1], values=values)


def intersect_constraints(left: ImageConstraints | None, right: ImageConstraints | None) -> ImageConstraints | None:
    if left is None or left.wildcard:
        return right
    if right is None or right.wildcard:
        return left
    sets = []
    for a in left.sets:
        for b in right.sets:
            x, y = _intersection_range(a.x, b.x), _intersection_range(a.y, b.y)
            if x is None or y is None:
                continue
            par = a.par or b.par
            if a.par is not None and b.par is not None:
                par = max(a.par[0], b.par[0]), min(a.par[1], b.par[1])
                if par[0] >= par[1]:
                    continue
            candidate = ImageSet(x, y, min(a.q, b.q), par)
            if candidate not in sets:
                sets.append(candidate)
            if len(sets) >= MAX_SETS:
                return ImageConstraints(tuple(sets))
    return ImageConstraints(tuple(sets))
