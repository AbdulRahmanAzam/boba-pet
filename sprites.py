"""Pixel-art pet characters (32x32), composed from layers at runtime.

Each part is a filled shape; outlines are generated automatically around
every part, so overlapping parts get a dark separating line, like hand-drawn
pixel art. Frames are cached as Tk PhotoImages scaled up with zoom().

Add a character by dropping a `sprite_<name>.py` file next to this one with
`compose_<name>(...)`, an optional `NAME` for the menu and an optional
`EXTRA_PALETTE` dict; it is picked up automatically on the next start.
"""

import importlib
import tkinter as tk
from pathlib import Path

SIZE = 32
SCALE = 3

PALETTE = {
    "K": "#211F24",  # outline
    "H": "#2B2833",  # hat
    "h": "#4A4556",  # hat highlight
    "B": "#D97757",  # hat band (Claude orange)
    "W": "#FEF5E6",  # cream fur
    "O": "#FDD5B5",  # peach fur
    "S": "#E6B4A0",  # stripes
    "P": "#EB9486",  # blush / inner ear
    "E": "#121014",  # eyes
    "w": "#FFFFFF",  # eye shine
    "C": "#C9A27C",  # trench coat
    "c": "#A8835F",  # coat shadow / belt
    "G": "#E8B84A",  # brass (buckle, magnifier rim)
    "L": "#BFE3F5",  # lens
    "R": "#6B4A2F",  # handle
    "M": "#7A3B3B",  # open mouth
    "T": "#F29AA0",  # tongue
    "Z": "#8A8780",  # zzz
    "A": "#E5484D",  # sound waves / red LED
    # Jar-Bot
    "b": "#B85C3E",  # torso shadow
    "D": "#3A3540",  # dark panel / treads
    "Y": "#9AA3AE",  # metal
    "y2": "#C8CFD8",  # metal highlight
    "j": "#D6F1F7",  # jar glass
    "a": "#7A3B3B",  # antenna bulb off
    "g": "#5BE37D",  # green LED
    "y": "#FFD45A",  # yellow LED
    "s0": "#FFD45A",  # spark body
    "s1": "#FFF0B3",  # spark glow
    "s2": "#D9D3B8",  # sleeping glow
    "s3": "#C2A55A",  # sleeping spark
    "m": "#C9762E",  # spark mouth
    # Agent Floppy
    "N": "#2F3B5C",  # disk body
    "n": "#1E2740",  # notch
    "U": "#6B5136",  # magnetic disk
    "V": "#C9CED6",  # shutter / earpiece
    # Shellby
    "Q": "#B9A3E3",  # snail body
    "q": "#9A84C9",  # snail shade
    "X": "#E7DCC3",  # CRT case
    "x": "#CDBF9F",  # CRT case shade
    "k": "#12352A",  # screen
    "k2": "#0B1F18",  # screen off
    "k3": "#2C5A49",  # screen glare
    "v": "#6CFF9A",  # phosphor green
    # mood marks drawn over any character
    "dr": "#7CC4F5",  # sweat drop
    "dr2": "#D8F0FF",  # drop shine
}

Grid = list[list[str | None]]
Pixels = set[tuple[int, int]]


def rect(x0: int, y0: int, x1: int, y1: int) -> Pixels:
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def rows(spans: dict[int, tuple[int, int]]) -> Pixels:
    """Shape from {row: (x_start, x_end)}."""
    return {(x, y) for y, (a, b) in spans.items() for x in range(a, b + 1)}


def mirror(px: Pixels) -> Pixels:
    return {(SIZE - 1 - x, y) for x, y in px}


def gaze_shift(eyes: object) -> tuple[int, int]:
    """Pupil offset (dx, dy) for a gaze like "left", "up" or "down_right"."""
    parts = eyes.split("_") if isinstance(eyes, str) else []
    dx = -1 if "left" in parts else 1 if "right" in parts else 0
    dy = -1 if "up" in parts else 1 if "down" in parts else 0
    return dx, dy


class Canvas:
    def __init__(self) -> None:
        self.grid: Grid = [[None] * SIZE for _ in range(SIZE)]

    def part(self, px: Pixels, color: str, outline: bool = True) -> None:
        """Paint a filled part, with an auto outline drawn first."""
        if outline:
            for x, y in px:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if (nx, ny) not in px and 0 <= nx < SIZE and 0 <= ny < SIZE:
                        self.grid[ny][nx] = "K"
        self.dots(px, color)

    def dots(self, px: Pixels, color: str) -> None:
        for x, y in px:
            if 0 <= x < SIZE and 0 <= y < SIZE:
                self.grid[y][x] = color


# ---------- shapes ----------
HEAD = rows({9: (9, 22), 10: (7, 24), 11: (6, 25), 12: (6, 25), 13: (6, 25),
             14: (6, 25), 15: (6, 25), 16: (7, 24), 17: (8, 23), 18: (10, 21)})
EAR_L = rows({4: (7, 7), 5: (7, 8), 6: (7, 9), 7: (7, 10), 8: (7, 11), 9: (7, 10)})
EAR_R = mirror(EAR_L)
INNER_EAR = {(8, 6), (8, 7), (9, 7), (8, 8)}
HEAD_STRIPES = {(13, 10), (13, 11), (16, 10), (16, 11), (16, 12), (19, 10), (19, 11)}

BODY = rows({19: (10, 21), 20: (9, 22), 21: (9, 22), 22: (8, 23), 23: (8, 23),
             24: (8, 23), 25: (8, 23), 26: (8, 23), 27: (8, 23), 28: (8, 23)})
CHEST = rows({19: (14, 17), 20: (14, 17), 21: (15, 16), 22: (15, 16)})
LAPEL = {(13, 19), (13, 20), (14, 21), (18, 19), (18, 20), (17, 21)}
BELT = rect(8, 24, 23, 24)
BUCKLE = {(15, 24), (16, 24)}
COAT_SHADOW = {(9, 25), (9, 26), (9, 27), (22, 25), (22, 26), (22, 27)}
PAWS = rows({28: (9, 14), 29: (8, 14)}) | rows({28: (17, 22), 29: (17, 23)})
PAW_SPLIT = {(11, 29), (20, 29)}

HAT_CROWN = rows({1: (12, 19), 2: (10, 21), 3: (10, 21), 4: (10, 21), 5: (10, 21)})
HAT_DENT: Pixels = set()
HAT_SHINE = {(12, 2), (12, 3)}
HAT_BAND = rect(10, 6, 21, 6)
HAT_BRIM = rows({7: (5, 26), 8: (6, 25)})

# Tail poses, curling up on the right side of the body.
TAILS = [
    {(24, 27), (25, 27), (25, 26), (26, 26), (26, 25), (27, 25), (27, 24),
     (27, 23), (28, 23), (28, 22), (27, 22)},
    {(24, 27), (25, 27), (25, 26), (26, 26), (26, 25), (27, 25), (27, 24),
     (28, 24), (28, 23), (29, 23), (29, 22)},
    {(24, 27), (25, 27), (26, 27), (26, 26), (27, 26), (27, 25), (28, 25),
     (28, 24), (29, 24), (29, 23), (30, 23)},
    {(24, 27), (25, 27), (25, 26), (26, 26), (26, 25), (26, 24), (27, 24),
     (27, 23), (27, 22), (26, 21), (27, 21)},
]
TAIL_STRIPE = {(26, 25), (27, 23)}

EYES = {
    "open": (rect(11, 12, 12, 14) | rect(19, 12, 20, 14), {(11, 12), (19, 12)}),
    "left": (rect(10, 12, 11, 14) | rect(18, 12, 19, 14), {(10, 12), (18, 12)}),
    "right": (rect(12, 12, 13, 14) | rect(20, 12, 21, 14), {(12, 12), (20, 12)}),
    "blink": ({(10, 13), (11, 13), (12, 13), (19, 13), (20, 13), (21, 13)}, set()),
    "happy": ({(10, 13), (11, 12), (12, 13), (19, 13), (20, 12), (21, 13)}, set()),
    "sleep": ({(10, 13), (11, 14), (12, 13), (19, 13), (20, 14), (21, 13)}, set()),
    "wide": (rect(10, 11, 12, 14) | rect(19, 11, 21, 14),
             {(10, 11), (11, 11), (19, 11), (20, 11)}),
}


def _gaze_eyes(dx: int, dy: int) -> tuple[Pixels, Pixels]:
    """Pupils shifted one pixel toward where the cat is looking."""
    shape = (rect(11 + dx, 12 + dy, 12 + dx, 14 + dy)
             | rect(19 + dx, 12 + dy, 20 + dx, 14 + dy))
    return shape, {(11 + dx, 12 + dy), (19 + dx, 12 + dy)}


for _v, _dy in (("up", -1), ("", 0), ("down", 1)):
    for _h, _dx in (("left", -1), ("", 0), ("right", 1)):
        _name = "_".join(n for n in (_v, _h) if n)
        if _name:
            EYES[_name] = _gaze_eyes(_dx, _dy)
BLUSH = {(8, 15), (9, 15), (22, 15), (23, 15), (8, 16), (23, 16)}
NOSE = {(15, 15), (16, 15)}
MOUTH_CLOSED = {(14, 16), (15, 17), (16, 17), (17, 16)}
MOUTH_OPEN = rows({16: (14, 17), 17: (14, 17)})
TONGUE = {(15, 17), (16, 17)}

# Magnifying glass held up in the right paw (two positions to "search").
def magnifier(dx: int, dy: int) -> tuple[Pixels, Pixels, Pixels, Pixels]:
    ring = {(x + dx, y + dy) for x, y in
            rows({14: (24, 27), 15: (23, 28), 16: (23, 28), 17: (23, 28),
                  18: (23, 28), 19: (24, 27)})}
    lens = {(x + dx, y + dy) for x, y in rows({15: (24, 27), 16: (24, 27),
                                                17: (24, 27), 18: (24, 27)})}
    shine = {(24 + dx, 15 + dy), (25 + dx, 15 + dy), (24 + dx, 16 + dy)}
    handle = {(23 + dx, 20 + dy), (22 + dx, 20 + dy), (22 + dx, 21 + dy)}
    return ring, lens, shine, handle

PAW_UP = rows({21: (21, 22), 22: (20, 22)})
SOUND = [{(3, 12), (2, 13), (2, 14), (3, 15)},
         {(1, 11), (0, 12), (0, 13), (0, 14), (0, 15), (1, 16)}]
ZZZ = [{(26, 3), (27, 3), (28, 3), (27, 4), (26, 5), (27, 5), (28, 5)},
       {(29, 0), (30, 0), (30, 1), (29, 2), (30, 2)}]


def finish_overlays(cv: Canvas, sound: int, zzz: int) -> Grid:
    for i in range(sound):
        cv.dots(SOUND[i], "A")
    for i in range(zzz):
        cv.dots(ZZZ[i], "Z")
    return cv.grid


def compose_cat(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                zzz: int = 0) -> Grid:
    """Detective cat: fedora, trench coat, magnifying glass when thinking."""
    cv = Canvas()
    cv.part(TAILS[tail % len(TAILS)], "O")
    cv.dots(TAIL_STRIPE & TAILS[tail % len(TAILS)], "S")

    cv.part(BODY, "C")
    cv.dots(COAT_SHADOW, "c")
    cv.part(CHEST, "W", outline=False)
    cv.dots(LAPEL, "c")
    cv.dots(BELT, "c")
    cv.dots(BUCKLE, "G")
    cv.part(PAWS, "W")
    cv.dots(PAW_SPLIT, "K")

    cv.part(EAR_L | EAR_R, "O")
    cv.dots(INNER_EAR | mirror(INNER_EAR), "P")
    cv.part(HEAD, "O")
    cv.dots(HEAD_STRIPES, "S")
    cv.dots(BLUSH, "P")
    shape, shine = EYES[eyes]
    cv.dots(shape, "E")
    cv.dots(shine, "w")
    cv.dots(NOSE, "P")
    if mouth == "open":
        cv.dots(MOUTH_OPEN, "M")
        cv.dots(TONGUE, "T")
    else:
        cv.dots(MOUTH_CLOSED, "K")

    lift = -hat_lift
    shift = lambda px: {(x, y + lift) for x, y in px}  # noqa: E731
    cv.part(shift(HAT_BRIM) | shift(HAT_CROWN) - shift(HAT_DENT), "H")
    cv.dots(shift(HAT_BAND), "B")
    cv.dots(shift(HAT_SHINE), "h")

    if busy is not None:
        ring, lens, glint, handle = magnifier(busy % 2, 0)
        cv.dots(handle, "R")
        cv.part(ring, "G")
        cv.dots(lens, "L")
        cv.dots(glint, "w")
        cv.part(PAW_UP, "W")
    return finish_overlays(cv, sound, zzz)


# ---------- Jar-Bot: a spark creature living in a glass-jar robot head ----------
def compose_jarbot(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                   hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                   zzz: int = 0) -> Grid:
    cv = Canvas()
    asleep = eyes == "sleep"
    # tank treads; the tread marks roll while thinking
    cv.part(rows({27: (9, 22), 28: (8, 23), 29: (8, 23), 30: (9, 22)}), "D")
    roll = (busy or 0) % 3
    cv.dots({(x, y) for x in range(9 + roll, 23, 3) for y in (28, 29)}, "h")
    # torso with chest panel and LEDs
    cv.part(rect(10, 19, 21, 26), "B")
    cv.dots(rect(10, 26, 21, 26), "b")
    cv.part(rect(13, 21, 19, 23), "D")
    lit = (busy if busy is not None else tail) % 3
    for i, (x, color) in enumerate(((14, "g"), (16, "y"), (18, "A"))):
        cv.dots({(x, 22)}, color if (i == lit and not asleep) else "h")
    # arms; the right one waves when happy
    cv.part(rect(7, 20, 9, 21) | rect(6, 22, 8, 23), "Y")
    if hat_lift:
        cv.part(rect(22, 17, 24, 18) | rect(23, 15, 25, 16), "Y")
    else:
        cv.part(rect(22, 20, 24, 21) | rect(23, 22, 25, 23), "Y")
    cv.part(rect(12, 18, 19, 18), "Y")  # neck
    # glass jar head, lid and antenna (lid pops up when happy)
    lid_y = 4 - hat_lift
    cv.part(rows({y: (9, 22) for y in range(6, 17)} | {17: (10, 21)}), "j")
    cv.dots({(10, y) for y in range(8, 15)} | {(11, 8)}, "w")
    cv.part(rect(10, lid_y, 21, lid_y + 1), "Y")
    cv.dots(rect(10, lid_y, 21, lid_y), "y2")
    cv.dots({(15, lid_y - 2), (15, lid_y - 1)}, "K")
    cv.part(rect(15, lid_y - 4, 16, lid_y - 3),
            "A" if (tail % 2 == 0 and not asleep) else "a")
    # the spark creature: glow halo + body + face; zips around while thinking
    dx = (-2, 0, 2, 0)[busy % 4] if busy is not None else 0
    dy = (0, 0, -1, -1)[tail % 4] if busy is None and not asleep else 0
    body = {(x + dx, y + dy) for x, y in
            rows({9: (13, 18), 10: (12, 19), 11: (12, 19), 12: (12, 19), 13: (13, 18)})}
    halo = {(x + ox, y + oy) for x, y in body
            for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1))} - body
    cv.dots(halo, "s2" if asleep else "s1")
    cv.dots(body, "s3" if asleep else "s0")
    ex, ey = 14 + dx, 10 + dy
    look, tilt = gaze_shift(eyes)
    if eyes in ("blink", "sleep"):
        cv.dots({(ex, ey + 1), (ex + 3, ey + 1)}, "E")
    elif eyes == "happy":
        cv.dots({(ex - 1, ey + 1), (ex, ey), (ex + 1, ey + 1),
                 (ex + 2, ey + 1), (ex + 3, ey), (ex + 4, ey + 1)}, "E")
    else:
        h = 3 if eyes == "wide" else 2
        top = ey + min(tilt, 0)  # looking down squints instead of hitting the mouth
        if tilt > 0:
            top, h = ey + 1, 1
        cv.dots({(ex + look, top + i) for i in range(h)}
                | {(ex + 3 + look, top + i) for i in range(h)}, "E")
    if mouth == "open":
        cv.dots(rect(15 + dx, 12 + dy, 16 + dx, 13 + dy), "M")
    elif not asleep:
        cv.dots({(15 + dx, 12 + dy), (16 + dx, 12 + dy)}, "m")
    return finish_overlays(cv, sound, zzz)


# ---------- Agent Floppy: a secret-agent floppy disk with shades ----------
def compose_floppy(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                   hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                   zzz: int = 0) -> Grid:
    cv = Canvas()
    # legs and shiny shoes
    cv.dots({(11, 28), (20, 28)}, "K")
    cv.part(rect(9, 29, 12, 30) | rect(19, 29, 22, 30), "E")
    cv.dots({(10, 29), (20, 29)}, "h")
    # arms with white gloves; right hand goes to the earpiece while listening
    cv.dots({(5, 19), (4, 20), (4, 21)}, "K")
    cv.part(rect(2, 22, 4, 23), "W")
    if sound:
        cv.dots({(26, 20), (27, 19)}, "K")
        cv.part(rect(27, 16, 29, 18), "W")
    else:
        cv.dots({(26, 19), (27, 20), (27, 21)}, "K")
        cv.part(rect(27, 22, 29, 23), "W")
    # disk body with the clipped corner and write-protect notch
    cv.part(rows({3: (6, 23), 4: (6, 24)} | {y: (6, 25) for y in range(5, 28)}), "N")
    cv.dots({(7, 26), (8, 26)}, "n")
    # metal shutter slides open while thinking, showing the magnetic disk
    cv.dots(rect(12, 4, 19, 9), "U")
    sx = (0, -3, -5, -3)[busy % 4] if busy is not None else 0
    cv.part(rect(11 + sx, 3, 20 + sx, 10), "V")
    cv.dots(rect(17 + sx, 4, 18 + sx, 9), "N")
    # paper label with an orange stripe
    cv.part(rect(8, 13, 23, 26), "W")
    cv.dots(rect(8, 13, 23, 14), "B")
    cv.dots({(9, 21), (10, 21), (21, 21), (22, 21)}, "P")
    # earpiece with a coiled wire; its LED blinks while listening
    cv.part({(26, 13), (27, 13), (27, 14)}, "V")
    cv.dots({(26, 15), (27, 16), (26, 17)}, "V")
    if sound and tail % 2 == 0:
        cv.dots({(27, 13)}, "A")
    # shades: pushed up when happy, slipping down when asleep
    lens_y = {"happy": 15, "sleep": 19}.get(eyes, 17)
    if eyes == "happy":
        cv.dots({(10, 19), (11, 18), (12, 19), (19, 19), (20, 18), (21, 19)}, "E")
    if eyes == "sleep":
        cv.dots({(10, 17), (11, 17), (12, 17), (19, 17), (20, 17), (21, 17)}, "E")
    cv.dots(rect(9, lens_y, 14, lens_y + 2) | rect(17, lens_y, 22, lens_y + 2)
            | {(15, lens_y), (16, lens_y)}, "E")
    if eyes != "sleep":  # agents don't blink: a glint sweeps across the lenses
        gx = 10 + (tail % 4)
        cv.dots({(gx, lens_y), (gx + 8, lens_y)}, "h")
    if mouth == "open":
        cv.dots(rect(14, 22, 17, 23), "M")
        cv.dots({(15, 23), (16, 23)}, "T")
    else:  # smirk
        cv.dots({(14, 23), (15, 23), (16, 23), (17, 22)}, "K")
    return finish_overlays(cv, sound, zzz)


# ---------- Shellby: a snail whose shell is a retro CRT status screen ----------
def compose_snail(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                  hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                  zzz: int = 0) -> Grid:
    cv = Canvas()
    asleep = eyes == "sleep"
    sway = (0, 1, 0, -1)[tail % 4] if not asleep else 0
    # eye-stalk antennas with glowing tips
    for base_x, top_x in ((4, 3 + sway), (9, 10 + sway)):
        cv.dots({(base_x, 10), ((base_x + top_x) // 2, 9), (top_x, 8)}, "Q")
        cv.part(rect(top_x, 6, top_x + 1, 7), "k2" if asleep else "v")
    # slug body and head
    slug = rows({11: (3, 10)} | {y: (2, 11) for y in range(12, 22)}
                | {22: (2, 27), 23: (2, 29), 24: (2, 30), 25: (2, 30), 26: (3, 29)})
    cv.part(slug, "Q")
    cv.dots(rect(4, 26, 28, 26), "q")
    # face on the head
    look, tilt = gaze_shift(eyes)
    if eyes in ("blink", "sleep"):
        cv.dots({(4, 16), (5, 16), (8, 16), (9, 16)}, "E")
    elif eyes == "happy":
        cv.dots({(4, 16), (5, 15), (6, 16), (8, 16), (9, 15), (10, 16)}, "E")
    else:
        # looking down: pupils drop into the lower half (the mouth sits just below)
        top = (13 if eyes == "wide" else 14) + tilt
        bot = 16 + min(tilt, 0)
        cv.dots(rect(4 + look, top, 5 + look, bot) | rect(8 + look, top, 9 + look, bot), "E")
        if tilt <= 0:
            cv.dots({(4 + look, top), (8 + look, top)}, "w")
    cv.dots({(3, 18), (10, 18)}, "P")
    if mouth == "open":
        cv.dots(rect(6, 18, 7, 19), "M")
    else:
        cv.dots({(5, 18), (6, 19), (7, 19), (8, 18)}, "K")
    # CRT monitor shell (bobs up when happy)
    up = hat_lift
    cv.part(rect(13, 5 - up, 30, 21 - up), "X")
    cv.dots(rect(13, 20 - up, 30, 21 - up), "x")
    cv.dots(rect(15, 19 - up, 19, 19 - up), "B")
    cv.dots({(26, 19 - up), (28, 19 - up)}, "c")
    cv.part(rect(15, 7 - up, 28, 17 - up), "k2" if asleep else "k")
    if not asleep:
        cv.dots({(16, 8 - up), (17, 8 - up), (16, 9 - up)}, "k3")
    ox, oy = 15, 7 - up  # screen origin, 14x11 pixels

    def screen(px: Pixels) -> None:
        cv.dots({(ox + x, oy + y) for x, y in px}, "v")

    if asleep:
        pass
    elif busy is not None:  # loading bar
        screen({(x, 3) for x in range(1, 13)} | {(x, 7) for x in range(1, 13)}
               | rect(1, 4, 1, 6) | rect(12, 4, 12, 6))
        screen(rect(3, 5, 3 + 2 * (busy % 4) + 1, 5))
    elif sound:  # live waveform
        heights = (1, 3, 2, 5, 1, 3, 5, 2, 1, 3, 4, 2)
        for i in range(12):
            h = heights[(i + tail) % len(heights)]
            screen({(1 + i, 5 - h // 2 + j) for j in range(h)})
    elif mouth == "open":  # text scrolling out
        widths = (10, 6, 11, 4, 8)
        for r in range(4):
            screen({(1 + x, 2 + r * 2) for x in range(widths[(r + tail) % 5])})
    elif eyes == "happy":  # heart
        screen({(4, 2), (5, 2), (8, 2), (9, 2)} | rect(3, 3, 10, 4)
               | rect(4, 5, 9, 5) | rect(5, 6, 8, 6) | {(6, 7), (7, 7)})
    else:  # prompt with a blinking cursor
        screen({(2, 3), (3, 4), (4, 5), (3, 6), (2, 7)})
        if tail % 2 == 0:
            screen(rect(6, 7, 8, 7))
    return finish_overlays(cv, 0, zzz)  # waveform is on the screen


CHARACTERS = {
    "Detective Cat": compose_cat,
    "Jar-Bot": compose_jarbot,
    "Agent Floppy": compose_floppy,
    "Shellby (CRT Snail)": compose_snail,
}
DEFAULT_CHARACTER = "Detective Cat"


def load_plugin_characters() -> None:
    """Register every sprite_<name>.py found next to this file."""
    for path in sorted(Path(__file__).resolve().parent.glob("sprite_*.py")):
        key = path.stem.removeprefix("sprite_")
        try:
            mod = importlib.import_module(path.stem)
            fn = getattr(mod, f"compose_{key}")
        except Exception:  # a broken plugin must not take the pet down
            continue
        PALETTE.update(getattr(mod, "EXTRA_PALETTE", {}))
        name = getattr(mod, "NAME", key.replace("_", " ").title())
        CHARACTERS[name] = fn
        if getattr(mod, "FACING", None):
            FACING[name] = mod.FACING


# The way each character's art faces (its head, or the side away from its
# tail). Walking the other way mirrors the frame so it faces where it goes.
# Characters not listed are drawn front-on and never mirror.
FACING = {"Detective Cat": "left", "Shellby (CRT Snail)": "left",
          "Blue Bot": "right", "Gentleman": "right", "Goggles": "right"}
SCREEN = {"k", "k2", "k3", "v"}  # snail CRT pixels, kept readable when mirrored


def flipped(grid: Grid) -> Grid:
    """Mirror a frame left-right, then un-mirror any CRT screen (text stays readable)."""
    out = [row[::-1] for row in grid]
    for row in out:
        cols = [x for x, c in enumerate(row) if c in SCREEN]
        if len(cols) > 2:
            a, b = cols[0], cols[-1]
            row[a:b + 1] = row[a:b + 1][::-1]
    return out


# Mood marks, drawn on top of any character after mirroring (so a "!" never
# turns around). They sit in the corners, where the characters leave room.
MARKS = {
    # "!" for "Claude Code needs you"
    "alert": [(rect(27, 0, 28, 4), "A"), (rect(27, 6, 28, 7), "A")],
    # sweat drop for "something went wrong"
    "sweat": [({(4, 8), (3, 9), (4, 9), (3, 10), (4, 10), (5, 10), (4, 11)}, "dr"),
              ({(3, 10)}, "dr2")],
}
THINK_DOTS = [rect(22, 3, 23, 4), rect(25, 2, 26, 3), rect(28, 1, 29, 2)]
SPARKLES = [{(3, 2), (2, 3), (3, 3), (4, 3), (3, 4)},
            {(28, 5), (27, 6), (28, 6), (29, 6), (28, 7)}]


def add_mark(grid: Grid, mark: str, frame: int) -> Grid:
    """Paint a mood mark ("think", "alert", "sweat", "sparkle") on a frame.

    Marks get a dark outline only where the frame is empty, so they never
    cut into the character.
    """
    if mark == "think":  # "..." thought dots filling in one by one
        layers = [(px, "W") for px in THINK_DOTS[:frame % 4]]
    elif mark == "sparkle":
        layers = [(SPARKLES[frame % 2], "y")]
    elif mark == "alert" and frame % 2:  # blinks
        layers = []
    else:
        layers = MARKS.get(mark, [])
    grid = [row[:] for row in grid]
    for px, color in layers:
        for x, y in px:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < SIZE and 0 <= ny < SIZE and grid[ny][nx] is None:
                    grid[ny][nx] = "K"
        for x, y in px:
            if 0 <= x < SIZE and 0 <= y < SIZE:
                grid[y][x] = color
    return grid


def compose(character: str = DEFAULT_CHARACTER, **pose: object) -> Grid:
    """Build one 32x32 frame for the given character and pose.

    Besides the character's own pose options, `facing` mirrors the frame and
    `mark` / `mark_f` add a mood mark (see add_mark) and its animation frame.
    """
    facing = pose.pop("facing", None)
    mark = pose.pop("mark", None)
    mark_f = int(pose.pop("mark_f", 0) or 0)
    flip = bool(facing) and FACING.get(character, facing) != facing
    eyes = pose.get("eyes")
    if flip and isinstance(eyes, str):  # the mirror turns left/right back
        swap = {"left": "right", "right": "left"}
        pose["eyes"] = "_".join(swap.get(p, p) for p in eyes.split("_"))
    grid = CHARACTERS.get(character, compose_cat)(**pose)  # type: ignore[arg-type]
    if flip:
        grid = flipped(grid)
    return add_mark(grid, mark, mark_f) if mark else grid


class SpriteCache:
    """Turns composed frames into scaled Tk images, built once per pose."""

    def __init__(self, transparent: str, scale: int = SCALE,
                 rim: str | None = None, rim_px: int = 1) -> None:
        self.transparent = transparent
        self.scale = scale
        self.rim = rim  # light outline color, so the pet shows on dark wallpapers
        self.rim_px = rim_px
        self.cache: dict[tuple, tk.PhotoImage] = {}

    def set_rim(self, rim: str | None) -> None:
        if rim != self.rim:
            self.rim = rim
            self.cache.clear()

    def get(self, **pose: object) -> tk.PhotoImage:
        key = tuple(sorted(pose.items()))
        if key not in self.cache:
            grid = compose(**pose)  # type: ignore[arg-type]
            self.cache[key] = self.outlined(grid) if self.rim else self.plain(grid)
        return self.cache[key]

    def plain(self, grid: Grid) -> tk.PhotoImage:
        img = tk.PhotoImage(width=SIZE, height=SIZE)
        img.put(" ".join(
            "{" + " ".join(PALETTE[c] if c else self.transparent for c in row) + "}"
            for row in grid))
        return img.zoom(self.scale)

    def outlined(self, grid: Grid) -> tk.PhotoImage:
        """Scaled frame with a thin rim drawn at screen resolution.

        The rim is rim_px screen pixels wide (not one sprite pixel, which
        would look chunky) and hugs the outside of the silhouette.
        """
        s, n, w = self.scale, SIZE * self.scale, self.rim_px
        if w == 1:  # plus-shaped reach keeps 1px corners crisp
            reach = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        else:
            reach = [(dx, dy) for dx in range(-w, w + 1) for dy in range(-w, w + 1)
                     if (dx or dy) and dx * dx + dy * dy <= w * w + 1]
        colors = [[PALETTE[c] if c else None for c in row] for row in grid]
        pixels = [[colors[y // s][x // s] for x in range(n)] for y in range(n)]
        for y in range(n):
            row = pixels[y]
            for x in range(n):
                if row[x] is None or row[x] == self.rim:
                    continue
                for dx, dy in reach:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < n and 0 <= ny < n and pixels[ny][nx] is None:
                        pixels[ny][nx] = self.rim
        img = tk.PhotoImage(width=n, height=n)
        img.put(" ".join("{" + " ".join(c or self.transparent for c in row) + "}"
                         for row in pixels))
        return img


load_plugin_characters()
