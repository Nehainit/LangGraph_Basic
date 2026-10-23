"""Caption styles and text stickers for the Edits tab, drawn as transparent full-frame PNGs.

The export lays each PNG over the picture for its time span. Sizes and positions are fractions of the frame, the
same numbers the editor's live preview uses (frontend/edits.js), so what people see is what they export.
"""

from pathlib import Path

from video_automation.agents.editor_agent import _font as unicode_font

# Fonts by role: macOS first (local development), then the DejaVu fonts the Docker image installs.
FONTS = {
    "regular": ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "serif": ["/System/Library/Fonts/Supplemental/Georgia Italic.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"],
    "mono": ["/System/Library/Fonts/Supplemental/Courier New Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
}
# Caption size as a fraction of frame width, and where the caption block is centred, as a fraction of height.
CAPTION_SIZE = {"pop": 0.06, "karaoke": 0.054, "boxed": 0.05, "minimal": 0.041, "serif": 0.054}
CAPTION_Y = {"top": 0.16, "middle": 0.5, "bottom": 0.82}
# Text sticker size: this fraction of frame height at size 1.
TEXT_SIZE = 0.032
YELLOW = (255, 210, 63, 255)
INK = (20, 20, 19, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)


def _font(role: str, size: int, text: str):
    from PIL import ImageFont

    # Latin fonts lack Devanagari and other scripts; the editor's Unicode font covers them.
    if any(ord(char) > 0x24F for char in text):
        return unicode_font(size)
    for path in FONTS[role]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return unicode_font(size)


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha


def caption_words(subtitle: dict, aligned: list[dict]) -> list[dict]:
    """Each word of a caption with its spoken time: from the narration alignment when it matches, else spread evenly."""
    words = str(subtitle["text"]).split()
    start, end = float(subtitle["start_seconds"]), float(subtitle["end_seconds"])
    spoken = [word for word in aligned if start - 0.05 <= float(word["start_seconds"]) < end + 0.05]
    if len(spoken) == len(words):
        times = [(float(word["start_seconds"]), float(word["end_seconds"])) for word in spoken]
    else:
        step = (end - start) / max(1, len(words))
        times = [(start + index * step, start + (index + 1) * step) for index in range(len(words))]
    return [{"text": text, "start_seconds": round(begin, 3), "end_seconds": round(finish, 3)} for text, (begin, finish) in zip(words, times)]


def _lines(draw, words: list[str], font, max_width: int, stroke: int) -> list[list[int]]:
    """Word indexes per line, wrapped to the width."""
    lines, current = [], []
    for index, word in enumerate(words):
        candidate = " ".join(words[item] for item in [*current, index])
        if current and draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke)[2] > max_width:
            lines.append(current)
            current = []
        current.append(index)
    return [*lines, current] if current else lines


def draw_caption(words: list[str], active: int | None, style: str, position: str, width: int, height: int, path: Path) -> None:
    """One caption frame; `active` is the word being spoken, highlighted in the pop and word-highlight styles."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if style == "pop":
        words = [word.upper() for word in words]
    text = " ".join(words)
    size = max(18, int(width * CAPTION_SIZE[style]))
    font = _font("serif" if style == "serif" else "regular" if style in {"boxed", "minimal"} else "bold", size, text)
    stroke = {"pop": max(3, size // 12), "karaoke": max(2, size // 16)}.get(style, 0)
    margin = int(width * 0.06)
    pad_x, pad_y = int(size * 0.35), int(size * 0.18)
    line_height = int(size * 1.3)
    lines = _lines(draw, words, font, width - 2 * margin - 2 * pad_x, stroke)
    space = draw.textlength(" ", font=font)
    top = int(height * CAPTION_Y[position]) - line_height * len(lines) // 2
    for row, indexes in enumerate(lines):
        widths = [draw.textlength(words[index], font=font) for index in indexes]
        line_width = sum(widths) + space * (len(indexes) - 1)
        x = (width - line_width) / 2
        y = top + row * line_height
        if style == "boxed":
            draw.rounded_rectangle((x - pad_x, y - pad_y, x + line_width + pad_x, y + size + pad_y), radius=pad_x, fill=(0, 0, 0, 184))
        for index, word_width in zip(indexes, widths):
            fill = WHITE
            if style == "serif":
                fill = (255, 243, 214, 255)
            if index == active and style == "pop":
                fill = YELLOW
            if index == active and style == "karaoke":
                draw.rounded_rectangle((x - pad_y, y - pad_y // 2, x + word_width + pad_y, y + size + pad_y), radius=pad_y, fill=YELLOW)
                fill = INK
            if style in {"minimal", "serif"} or (style == "pop"):
                # A soft drop shadow keeps light text readable over bright frames.
                offset = max(2, size // 20)
                draw.text((x + offset, y + offset), words[index], font=font, fill=(0, 0, 0, 170))
            karaoke_word = index == active and style == "karaoke"
            draw.text(
                (x, y), words[index], font=font, fill=fill,
                stroke_width=0 if karaoke_word else stroke, stroke_fill=BLACK,
            )
            x += word_width + space
    image.save(path)


def caption_overlays(subtitles: list[dict], aligned: list[dict], style: str, position: str, width: int, height: int, out: Path) -> list[tuple[Path, float, float]]:
    if style == "off":
        return []
    out.mkdir(parents=True, exist_ok=True)
    overlays = []
    for number, subtitle in enumerate(subtitles, start=1):
        words = caption_words(subtitle, aligned)
        texts = [word["text"] for word in words]
        if style in {"pop", "karaoke"}:
            # One frame per spoken word, so the highlight moves with the narrator.
            for index, word in enumerate(words):
                path = out / f"caption_{number:03}_{index:02}.png"
                draw_caption(texts, index, style, position, width, height, path)
                finish = words[index + 1]["start_seconds"] if index + 1 < len(words) else float(subtitle["end_seconds"])
                begin = float(subtitle["start_seconds"]) if index == 0 else word["start_seconds"]
                overlays.append((path, begin, finish))
        else:
            path = out / f"caption_{number:03}.png"
            draw_caption(texts, None, style, position, width, height, path)
            overlays.append((path, float(subtitle["start_seconds"]), float(subtitle["end_seconds"])))
    return overlays


def draw_text(item: dict, width: int, height: int, path: Path) -> None:
    """One text sticker in its style, centred on its (x, y) position."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    style, text, color = item["style"], item["text"], _rgba(item["color"])
    size = max(14, int(height * TEXT_SIZE * float(item["size"])))
    font = _font({"serif": "serif", "typewriter": "mono"}.get(style, "bold"), size, text)
    stroke = max(2, size // 14) if style in {"bold", "outline"} else 0
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    text_width, text_height = box[2] - box[0], box[3] - box[1]
    x = float(item["x"]) * width - text_width / 2 - box[0]
    y = float(item["y"]) * height - text_height / 2 - box[1]
    pad_x, pad_y = int(size * 0.45), int(size * 0.25)
    rectangle = (x + box[0] - pad_x, y + box[1] - pad_y, x + box[2] + pad_x, y + box[3] + pad_y)
    if style == "highlight":
        draw.rounded_rectangle(rectangle, radius=int(size * 0.3), fill=color)
        draw.text((x, y), text, font=font, fill=INK)
    elif style == "label":
        draw.rounded_rectangle(rectangle, radius=int(size * 0.2), fill=(0, 0, 0, 178))
        draw.text((x, y), text, font=font, fill=color)
    elif style == "outline":
        draw.text((x, y), text, font=font, fill=(0, 0, 0, 0), stroke_width=stroke, stroke_fill=color)
    elif style == "bold":
        offset = max(2, size // 16)
        draw.text((x, y + offset), text, font=font, fill=BLACK, stroke_width=stroke, stroke_fill=BLACK)
        draw.text((x, y), text, font=font, fill=color, stroke_width=stroke, stroke_fill=BLACK)
    else:
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 150))
        draw.text((x, y), text, font=font, fill=color)
    image.save(path)


def text_overlays(texts: list[dict], width: int, height: int, out: Path) -> list[tuple[Path, float, float]]:
    out.mkdir(parents=True, exist_ok=True)
    overlays = []
    for number, item in enumerate(texts, start=1):
        if not str(item["text"]).strip():
            continue
        path = out / f"text_{number:03}.png"
        draw_text(item, width, height, path)
        overlays.append((path, float(item["start_seconds"]), float(item["end_seconds"])))
    return overlays
