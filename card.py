# -*- coding: utf-8 -*-
"""The title card, drawn as a reddit post rather than set as a subtitle.

It used to be an ASS style: BorderStyle 3 paints an opaque box, which is a
title card with no image files and no PIL. That box could hold text and
nothing else - no avatar, no logo, no rounded corners - so the card read as a
subtitle with the background knocked out, and a subtitle is not a thing a
scrolling viewer stops for. A post is.

So the card is now a PNG per lit word, laid over the footage by render(). One
image per word rather than one animated file: ffmpeg reads APNG frame delays
as a fixed rate and plays the whole thing in a blink, and the highlight has to
follow the narrator, not a rate. `enable` on each overlay is what carries the
timing instead - see render._card_chain().

The whole title is on screen the entire time, which is the other half of what
changed. The pairs-of-words card before this one showed two words at a time,
so the opening frame was never the finished line and TikTok - which picks the
cover of an inbox draft itself, out of the opening - had two words to pick
from. Every frame here is the whole post, so every frame is a usable cover and
COVER_SEC has nothing left to protect.
"""
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import safety
import script
from config import CHANNEL, OUT_DIR, PART_WORD

log = logging.getLogger(__name__)

W, H = 1080, 1920
CARD_W = 900               # of 1080: enough margin that the corners read as corners
PAD = 48
RADIUS = 32
# Near-black rather than reddit's own #1A1A1B: the card sits on gameplay
# footage, and the extra contrast is what keeps the white type off it.
BG = (14, 14, 16, 255)
FG = (255, 255, 255, 255)
DIM = (150, 152, 156, 255)
# The lit word. Reddit's orange loses against the footage behind it and the
# old card's deep red was picked for a WHITE box - neither survives here.
ACCENT = (255, 59, 48, 255)
BLUE = (26, 138, 255, 255)      # the verified tick
ORANGE = (255, 69, 0, 255)      # reddit's
USER = "CheshireCat247"
# Reactions. Decoration, not data - they say "this post has been seen" and
# nothing else, so the row is fixed and never derived from the story.
EMOJI = "\U0001F383\U0001F63A\U0001F47B\U0001F480\U0001F921\U0001F608" \
        "\U0001F47D\U0001F916\U0001F431\U0001F640\U0001F639\U0001F63B"
STATS = "99+"

# The card scaling into place instead of simply being there. It was removed
# once, on 2026-08-14: TikTok picks the cover of an inbox draft itself, out of
# the opening, and it took a frame mid-scale - a cover with the post shrunk
# small. That risk is accepted now and this is where it lives, so if covers
# start coming back cropped, POP_SCALES is the first thing to empty.
# Overshoot then settle: a straight 0.8 -> 1.0 ramp reads as a slide, the 5%
# past the mark is what makes it land.
POP_SCALES = (0.82, 1.05)
POP_SEC = 0.07             # per frame - two of them is 140ms, under a blink

TITLE_MAX = 96             # px; above this a three-word title fills the card
TITLE_MIN = 34             # below this it is too small to read while scrolling
LINE = 1.22                # of the type size
HEAD_H, EMOJI_H, FOOT_H, GAP = 96, 56, 64, 34
PART_SIZE = 40

# Bold, and it has to carry Cyrillic. First one that exists wins, and Arial
# Bold leads both lists on purpose: it is on this desk AND on the runner, which
# is the only way a card drawn while writing the story looks like the card that
# ships. publish.yml installs it with the subtitle font; DejaVu is the fallback
# for a run where that install failed, exactly as it is for the subtitles.
# A runner with none of them gets a warning and PIL's bitmap default, which is
# unreadable at this size - a louder failure than a card quietly set in the
# wrong face, and it is meant to be.
FONT_CANDIDATES = ("C:/Windows/Fonts/arialbd.ttf",
                   "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "C:/Windows/Fonts/segoeuib.ttf")
EMOJI_CANDIDATES = ("C:/Windows/Fonts/seguiemj.ttf",
                    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")


def _font_path(candidates) -> str | None:
    return next((p for p in candidates if Path(p).exists()), None)


FONT = _font_path(FONT_CANDIDATES)
EMOJI_FONT = _font_path(EMOJI_CANDIDATES)


def _face(size: int):
    if not FONT:
        log.warning("no bold font found, the title card will be unreadable")
        return ImageFont.load_default()
    return ImageFont.truetype(FONT, size)


def _emoji_face(size: int):
    # Colour emoji are a nice-to-have: no font, no row, and the card is fine
    # without it. NotoColorEmoji is a bitmap face and only renders at 109px,
    # so it is scaled down from there rather than asked for a size it refuses.
    if not EMOJI_FONT:
        return None, 1.0
    if "Noto" in EMOJI_FONT:
        return ImageFont.truetype(EMOJI_FONT, 109), size / 109
    return ImageFont.truetype(EMOJI_FONT, size), 1.0


def _snoo(d, x, y, r):
    """Reddit's mark, drawn rather than shipped as a binary in assets/.

    Simplified on purpose: at the size it is drawn - a badge on the corner of
    an avatar - the mouth and the shading are two pixels of mud, so they are
    left out and the silhouette carries the recognition.
    """
    d.ellipse((x - r, y - r, x + r, y + r), fill=ORANGE)
    s, hy = r * 0.60, y + r * 0.16
    ear = s * 0.32
    for sx in (-1, 1):
        d.ellipse((x + sx * s * 0.82 - ear, hy - s * 0.62 - ear,
                   x + sx * s * 0.82 + ear, hy - s * 0.62 + ear), fill=FG)
    d.ellipse((x - s, hy - s * 0.78, x + s, hy + s * 0.78), fill=FG)
    d.line((x, hy - s * 0.6, x, y - r * 0.66), fill=FG, width=max(2, int(r * 0.11)))
    a = r * 0.16
    d.ellipse((x - a, y - r * 0.66 - a, x + a, y - r * 0.66 + a), fill=FG)
    e = s * 0.22
    for sx in (-1, 1):
        d.ellipse((x + sx * s * 0.40 - e, hy - e, x + sx * s * 0.40 + e, hy + e),
                  fill=ORANGE)


def _tick(d, x, y, r):
    d.ellipse((x - r, y - r, x + r, y + r), fill=BLUE)
    w = max(2, int(r * 0.24))
    d.line((x - r * 0.42, y + r * 0.02, x - r * 0.08, y + r * 0.38), fill=FG, width=w)
    d.line((x - r * 0.10, y + r * 0.38, x + r * 0.46, y - r * 0.34), fill=FG, width=w)


def _heart(d, x, y, r):
    d.ellipse((x - r, y - r * 0.85, x - r * 0.02, y + r * 0.25), fill=DIM)
    d.ellipse((x + r * 0.02, y - r * 0.85, x + r, y + r * 0.25), fill=DIM)
    d.polygon([(x - r * 0.97, y - r * 0.12), (x + r * 0.97, y - r * 0.12),
               (x, y + r)], fill=DIM)


def _bubble(d, x, y, r):
    d.rounded_rectangle((x - r, y - r * 0.85, x + r, y + r * 0.45),
                        radius=int(r * 0.35), fill=DIM)
    d.polygon([(x - r * 0.55, y + r * 0.4), (x - r * 0.15, y + r * 0.4),
               (x - r * 0.45, y + r)], fill=DIM)


def _share(d, x, y, r):
    w = max(3, int(r * 0.24))
    d.line((x, y - r, x, y + r * 0.15), fill=DIM, width=w)
    d.line((x, y - r), fill=DIM, width=w)
    d.line((x - r * 0.55, y - r * 0.45, x, y - r), fill=DIM, width=w)
    d.line((x, y - r, x + r * 0.55, y - r * 0.45), fill=DIM, width=w)
    d.arc((x - r * 0.85, y - r * 0.35, x + r * 0.85, y + r * 1.5), 180, 360,
          fill=DIM, width=w)


def _fit(d, tokens: list[str], inner: int, room: int):
    """Largest type at which the title wraps into `room` px, and its layout.

    Returns (font, size, lines) where a line is a list of (token_index, text).
    Tokens rather than words: a token is what lights up as one unit, and the
    layout has to know where each one sits to be able to light it.

    Walks down from TITLE_MAX because the hook wants the biggest type the box
    can hold, not a fixed one - a four-word title deserves the whole card. At
    TITLE_MIN it stops and lets the card grow taller than a square instead:
    the requirement is that every word is on screen, and a card that outgrows
    the square is a card that still reads.
    """
    for size in range(TITLE_MAX, TITLE_MIN - 1, -2):
        f = _face(size)
        lines, cur, cur_w = [], [], 0.0
        space = d.textlength(" ", font=f)
        over = False
        for i, t in enumerate(tokens):
            tw = d.textlength(t, font=f)
            if cur and cur_w + space + tw > inner:
                lines.append(cur)
                cur, cur_w = [], 0.0
            if tw > inner:
                over = True          # a single token wider than the box
            cur_w += (space if cur else 0) + tw
            cur.append((i, t))
        if cur:
            lines.append(cur)
        if not over and int(size * LINE) * len(lines) <= room:
            return f, size, lines
    return f, size, lines


def _draw(tokens: list[str], lit: int | None, part: int, channel: str) -> Image.Image:
    """One full 1080x1920 frame: the post, centred, with `lit` in ACCENT.

    Full frame rather than a cropped card so render() can overlay at 0:0 and
    never carry a position. The transparent margin costs nothing - it is a
    single flat colour and png packs it away.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inner = CARD_W - PAD * 2

    part_h = int(PART_SIZE * LINE) + GAP if part >= 1 else 0
    chrome = PAD * 2 + HEAD_H + GAP + EMOJI_H + GAP + FOOT_H + GAP + part_h
    f, size, lines = _fit(d, tokens, inner, CARD_W - chrome)
    lh = int(size * LINE)
    card_h = max(CARD_W, chrome + lh * len(lines))

    x0, y0 = (W - CARD_W) // 2, (H - card_h) // 2
    d.rounded_rectangle((x0, y0, x0 + CARD_W, y0 + card_h), radius=RADIUS, fill=BG)

    # --- poster
    ar = HEAD_H // 2
    ax, ay = x0 + PAD + ar, y0 + PAD + ar
    d.ellipse((ax - ar, ay - ar, ax + ar, ay + ar), fill=(60, 62, 68, 255))
    ef, scale = _emoji_face(int(ar * 1.35))
    if ef:
        _emoji(img, d, ef, scale, "\U0001F63A", ax, ay, centre=True)
    _snoo(d, ax + ar - 8, ay + ar - 8, 21)

    uf = _face(38)
    ux = ax + ar + 24
    d.text((ux, ay), USER, font=uf, anchor="lm", fill=FG)
    _tick(d, ux + d.textlength(USER, font=uf) + 26, ay, 16)
    d.text((x0 + CARD_W - PAD, ay - 6), "\u2022\u2022\u2022", font=uf, anchor="rm", fill=DIM)

    # --- reactions
    ey = y0 + PAD + HEAD_H + GAP + EMOJI_H // 2
    ef, scale = _emoji_face(EMOJI_H - 16)
    if ef:
        ex = x0 + PAD
        for ch in EMOJI:
            _emoji(img, d, ef, scale, ch, ex, ey)
            ex += EMOJI_H - 10

    # --- the title, token by token so exactly one of them can be lit
    ty = y0 + PAD + HEAD_H + GAP + EMOJI_H + GAP
    space = d.textlength(" ", font=f)
    for line in lines:
        # Ragged right, flush left - the way the post it is imitating sets its
        # own title, and the way a reader's eye already expects to find the
        # start of every line.
        cx = x0 + PAD
        for i, t in line:
            d.text((cx, ty), t, font=f, anchor="la",
                   fill=ACCENT if i == lit else FG)
            cx += d.textlength(t, font=f) + space
        ty += lh

    if part >= 1:
        word = PART_WORD.get(channel, PART_WORD["en"])
        d.text((x0 + PAD, ty + GAP // 2), f"{word} {part}", font=_face(PART_SIZE),
               anchor="la", fill=DIM)

    # --- counters
    fy = y0 + card_h - PAD - FOOT_H // 2
    sf = _face(32)
    fx = x0 + PAD + 20
    _heart(d, fx, fy, 18)
    d.text((fx + 36, fy), STATS, font=sf, anchor="lm", fill=DIM)
    fx += 156
    _bubble(d, fx, fy, 19)
    d.text((fx + 36, fy), STATS, font=sf, anchor="lm", fill=DIM)
    sx = x0 + CARD_W - PAD - 20
    d.text((sx, fy), "Share", font=sf, anchor="rm", fill=DIM)
    _share(d, sx - d.textlength("Share", font=sf) - 30, fy, 17)
    return img


def _emoji(img, d, font, scale: float, ch: str, x: int, y: int, centre=False):
    """Draw one colour glyph, scaling it when the face is a bitmap one.

    NotoColorEmoji only renders at 109px, so on Linux the glyph is drawn on
    its own layer and resampled down. Segoe UI Emoji is COLR and takes any
    size, so there scale is 1.0 and this is a plain draw.
    """
    if scale == 1.0:
        d.text((x, y), ch, font=font, anchor="mm" if centre else "lm",
               embedded_color=True)
        return
    layer = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((60, 60), ch, font=font, anchor="mm",
                               embedded_color=True)
    layer = layer.resize((int(120 * scale), int(120 * scale)), Image.LANCZOS)
    img.alpha_composite(layer, (int(x - (layer.width / 2 if centre else 0)),
                                int(y - layer.height / 2)))


def _zoom(img: Image.Image, s: float) -> Image.Image:
    """The same frame at `s` of its size, still centred on the same point.

    The whole 1080x1920 canvas is scaled rather than the card inside it, which
    works out identical - the card sits dead centre of the canvas - and saves
    laying the post out a second time per pop frame.
    """
    big = img.resize((round(W * s), round(H * s)), Image.LANCZOS)
    if s >= 1:
        x, y = (big.width - W) // 2, (big.height - H) // 2
        return big.crop((x, y, x + W, y + H))
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(big, ((W - big.width) // 2, (H - big.height) // 2))
    return out


def _accented(title: str, words: int) -> int | None:
    """Index of the word the title's [emphasis] sits on, or None for no accent.

    The mark is written in front of a word, so everything plain() leaves in
    front of the mark is what precedes that word - counting those words IS the
    index. Out of range means the tag trails the last word, which _title_fault()
    refuses, so this only ever guards a title that came from somewhere else.
    """
    i = title.lower().find("[emphasis")
    if i < 0:
        return None
    n = len(script.plain(title[:i]).split())
    return n if n < words else None


def build(words: list[dict], title: str, title_end: float, name: str,
          part: int = 0, channel: str = CHANNEL) -> list[tuple[float, float, Path]]:
    """(start, end, png) for the whole title card, one entry per lit word.

    `words` are the title's OWN take, timed from zero - the highlight follows
    the narrator rather than an estimate, so a word they linger on stays lit.
    Empty (an older render, or an aligner that came back with nothing) falls
    back to one unlit frame carrying `title`, which is still a whole post.

    Short function words are glued to the word after them before anything is
    drawn: a card is on screen for three seconds, and one frame of red on
    "\u0432" is a flicker, not an accent.
    """
    tokens = [safety.mask(script.plain(w["word"])).strip() for w in words]
    starts = [w["start"] for w in words]
    keep = [i for i, t in enumerate(tokens) if t]
    tokens, starts = [tokens[i] for i in keep], [starts[i] for i in keep]
    if tokens:
        # the take ends on a full stop so the title does not run into the
        # story; the card never showed that stop and still must not
        tokens[-1] = tokens[-1].rstrip(".!?") or tokens[-1]

    if not tokens:
        # The ordinary case now: the title is not narrated, so there are no word
        # timings to follow and the card is one still - the COVER, up for
        # voice.COVER_SEC. The [emphasis] is still drawn, and on a still frame it
        # matters more than it did while it was moving: it is placed by hand in
        # the review issue, on the word the line turns on, and it is the one word
        # the thumbnail leads with.
        plain = safety.mask(script.plain(title)).split()
        img = _draw(plain, _accented(title, len(plain)), part, channel)
        path = OUT_DIR / f"{name}_card0.png"
        img.save(path)
        return [(0.0, title_end, path)]

    # The card is up before the narrator says anything - voice.py holds the
    # narration back by the length of the whoosh - and through that stretch
    # NOTHING is lit: the highlight follows the voice, and a word burning red
    # while the viewer hears a sound effect is the card lying about which word
    # is being read. Skipped when the gap is too short to be a frame of its own.
    out, first = [], None
    if starts[0] > POP_SEC:
        first = _draw(tokens, None, part, channel)
        path = OUT_DIR / f"{name}_cardlead.png"
        first.save(path)
        out.append((0.0, round(starts[0], 2), path))

    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else title_end
        if not out and i == 0:
            start = 0.0                       # nothing precedes the first word
        if end <= start:
            continue                          # two words inside one frame
        path = OUT_DIR / f"{name}_card{i}.png"
        img = _draw(tokens, i, part, channel)
        img.save(path)
        first = first if first is not None else img
        out.append((round(start, 2), round(end, 2), path))

    # The pop eats the front of the first word's window rather than delaying
    # it: the highlight has to stay on the narrator, and a card that arrives
    # 140ms late is a card whose every word is 140ms late from then on.
    # Skipped when the first word is too short to give that time away - the
    # scale-up would then be most of what that word gets.
    span = POP_SEC * len(POP_SCALES)
    if out and out[0][1] - out[0][0] > span * 2:
        pop = []
        for i, s in enumerate(POP_SCALES):
            path = OUT_DIR / f"{name}_pop{i}.png"
            _zoom(first, s).save(path)
            pop.append((round(i * POP_SEC, 2), round((i + 1) * POP_SEC, 2), path))
        out[0] = (round(span, 2), out[0][1], out[0][2])
        out = pop + out
    return out


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def _w(t, s):
        return {"word": t, "start": s, "end": s + 0.4}

    _d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    _inner = CARD_W - PAD * 2

    # Every word is on screen or the card has failed at its one job. A title at
    # the ceiling script.MAX_TITLE_WORDS allows, in long words, is the case
    # that would silently drop one.
    _long = ["\u043f\u0440\u0435\u0434\u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u043e"] * 12
    _f, _size, _lines = _fit(_d, _long, _inner, CARD_W)
    assert [i for l in _lines for i, _ in l] == list(range(12)), _lines
    assert _size >= TITLE_MIN, _size
    # ...and no line may overhang the box it is set in
    _space = _d.textlength(" ", font=_f)
    for _l in _lines:
        assert sum(_d.textlength(t, font=_f) for _, t in _l) \
            + _space * (len(_l) - 1) <= _inner, _l

    # A short title takes the biggest type on offer rather than a fixed one -
    # this is the whole reason _fit walks down instead of using one size.
    assert _fit(_d, ["\u0416\u0435\u043d\u0430", "\u0443\u0448\u043b\u0430"], _inner, CARD_W)[1] == TITLE_MAX

    # One frame per word, back to back, ending on the card itself. A gap
    # between two frames is a frame of bare footage in the middle of the hook.
    # A title that starts on the first frame - no whoosh in front of it - has
    # no unlit stretch to show, so the first word owns the opening.
    _tw = [_w("\u0421\u043e\u0441\u0435\u0434\u043a\u0430", 0.02), _w("\u043f\u0440\u0438\u0441\u043b\u0430\u043b\u0430", 0.7), _w("\u0441\u0447\u0451\u0442.", 1.2)]
    _cards = build(_tw, "", 2.0, "_check")
    _pop, _words = _cards[:len(POP_SCALES)], _cards[len(POP_SCALES):]
    assert len(_words) == 3, _cards
    # the card IS the first frame of the video, popped or not - a gap here is
    # bare footage before the hook
    assert _cards[0][0] == 0.0, _cards[0]
    assert [round(e, 2) for _, e, _ in _words] == [0.7, 1.2, 2.0], _words
    assert all(a[1] == b[0] for a, b in zip(_cards, _cards[1:])), _cards
    assert all(p.exists() for _, _, p in _cards)
    # the pop eats the front of the first word rather than pushing it back:
    # every window after it has to stay where the narrator put it
    assert _words[0][0] == round(POP_SEC * len(POP_SCALES), 2), _words[0]
    # every pop frame is a different size, and none of them is the settled one
    _sizes = {p.stat().st_size for _, _, p in _pop}
    assert len(_sizes) == len(POP_SCALES), _sizes
    assert all(p.stat().st_size not in {q.stat().st_size for _, _, q in _words}
               for _, _, p in _pop), "a pop frame is the settled card"

    # Words that share a frame must not leave a zero-length one behind - an
    # overlay enabled on an empty window is an overlay ffmpeg still decodes.
    _tight = build([_w("\u0410", 0.0), _w("\u0432\u043e\u0442", 0.0), _w("\u0438", 1.0)], "", 1.5, "_check")
    assert len(_tight) == 2 + len(POP_SCALES), _tight

    # A first word too short to lend the pop its front goes without one - the
    # scale-up must never be most of what a word gets.
    assert len(build([_w("\u0410", 0.0), _w("\u043f\u043e\u0442\u043e\u043c", 0.1)], "", 1.0, "_check")) == 2

    # With the whoosh in front, the opening belongs to an UNLIT card: the voice
    # has not reached the first word yet, so nothing may be red. This is the
    # case that broke when voice.py started delaying the narration.
    _led = build([_w("\u0421\u043e\u0441\u0435\u0434\u043a\u0430", 0.7), _w("\u043f\u0440\u0438\u0441\u043b\u0430\u043b\u0430", 1.2)], "", 2.0, "_check")
    assert len(_led) == len(POP_SCALES) + 3, _led
    _unlit = _led[len(POP_SCALES)]
    assert _unlit[2].name.endswith("_cardlead.png"), _led
    assert _unlit[1] == 0.7, _led                # it ends where the voice starts
    # the pop rides the unlit frame - the card appears blank and the words
    # light up afterwards, rather than arriving with one already burning
    assert _led[0][2].name.endswith("_pop0.png"), _led
    assert [round(s, 2) for s, _, _ in _led[len(POP_SCALES):]] == [0.14, 0.7, 1.2], _led
    assert all(a[1] == b[0] for a, b in zip(_led, _led[1:])), _led

    # "Часть N" is the renderer's alone: it is on the card and in no narrated
    # text anywhere, which is the only reason it can be shown without being
    # read out. Compared as pixels because that is the only place it exists.
    _plain = _draw(["Жена", "ушла"], None, 0, "ru").tobytes()
    assert _draw(["Жена", "ушла"], None, 2, "ru").tobytes() != _plain
    assert _draw(["Жена", "ушла"], None, 0, "ru").tobytes() == _plain
    # ...and the lit word has to actually change the picture, or every frame of
    # the card is the same frame and the highlight does nothing
    assert _draw(["Жена", "ушла"], 1, 0, "ru").tobytes() != _plain

    # No timings at all: still a post, still the whole title, just unlit.
    _fb = build([], "\u0416\u0435\u043d\u0430 \u0443\u0448\u043b\u0430 \u043a \u0441\u043e\u0441\u0435\u0434\u0443", 1.8, "_check")
    assert len(_fb) == 1 and _fb[0] == (0.0, 1.8, OUT_DIR / "_check_card0.png"), _fb

    _demo = OUT_DIR / "_card_demo.png"
    _draw("\u0421\u043e\u0441\u0435\u0434\u043a\u0430 \u043f\u0440\u0438\u0441\u043b\u0430\u043b\u0430 \u0441\u0447\u0451\u0442 \u043d\u0430 80000 \u0437\u0430 \u043f\u043e\u0442\u043e\u043f".split(),
          3, 2, "ru").save(_demo)
    log.info("ok - %s", _demo)
