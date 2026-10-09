"""Goggles: a spiky-haired kid inventor with brass aviator goggles and a toy blaster.

Original 32x32 chibi sprite, composed from layers like the characters in
sprites.py (auto-outlined parts, then flat detail dots on top).
"""

from sprites import Canvas, Grid, Pixels, finish_overlays, gaze_shift, mirror, rect, rows

NAME = "Goggles"  # label in the Character menu

EXTRA_PALETTE: dict[str, str] = {
    "gg_hair": "#F29A2E",     # flame hair
    "gg_hair_lt": "#FFD45A",  # hair highlight / flame tips
    "gg_hair_dk": "#C9621F",  # hair shadow
    "gg_strap": "#5C3B25",    # goggle strap / belt leather
    "gg_coat": "#4B4E57",     # dark grey jacket
    "gg_coat_dk": "#33353C",  # jacket shadow / trousers
    "gg_trim": "#F28C28",     # orange jacket trim
    "gg_glove": "#FFC93C",    # yellow gloves
    "gg_boot": "#8A5A2B",     # brown boots
    "gg_blast": "#6F8FB0",    # blaster body (toy blue-grey)
    "gg_blast_dk": "#4E6A88", # blaster underside
    "gg_charge": "#7DF0FF",   # blaster charge glow
}


def spike(tx: int, ty: int, by: int, lean: int) -> Pixels:
    """Flame spike from tip (tx, ty) widening down to row by; lean tilts the tip."""
    px: Pixels = set()
    for y in range(ty, by + 1):
        d = y - ty
        cx = tx - (lean * d) // 3
        half = (d + 1) // 2
        px |= {(x, y) for x in range(cx - half, cx + half + 1)}
    return px


# ---------- hair ----------
# Flame spikes as (tip_x, tip_y, base_row, lean); the outer ones sweep outward.
SPIKES = [(3, 6, 10, -5), (7, 2, 8, -3), (12, 2, 7, -1), (17, 2, 7, 1),
          (23, 2, 8, 3), (28, 6, 10, 5)]
HAIR_DOME = rows({5: (10, 21), 6: (8, 23), 7: (7, 24), 8: (6, 25), 9: (6, 25)})
SIDEBURNS = rows({10: (6, 8), 11: (6, 7), 12: (6, 6)})
SIDEBURNS = SIDEBURNS | mirror(SIDEBURNS)
HAIR_SPIKES: Pixels = set()
for _spike in SPIKES:
    HAIR_SPIKES |= spike(*_spike)
HAIR = HAIR_DOME | SIDEBURNS | HAIR_SPIKES
# Bangs: zigzag fringe dipping over the forehead, shaded underneath.
BANGS = {(x, 10) for x in (9, 10, 12, 13, 15, 16, 18, 19, 21, 22)}
HAIR_SHADE = rows({9: (8, 23)}) | {(x, 10) for x in (9, 12, 15, 18, 21)}


def spike_core(i: int) -> Pixels:
    """Light streak down the middle of spike i, just below its tip."""
    tx, ty, _by, lean = SPIKES[i]
    return {(tx - (lean * d) // 3, ty + d) for d in (1, 2, 3)}


def flame_lick(i: int) -> Pixels:
    """One-pixel lick of flame above the tip of spike i (leans with the spike)."""
    tx, ty, _by, lean = SPIKES[i]
    return {(tx + (lean > 2) - (lean < -2), ty - 1)}


# ---------- head ----------
HEAD = rows({7: (9, 22), 8: (8, 23)} | {y: (7, 24) for y in range(9, 17)}
            | {17: (8, 23), 18: (9, 22), 19: (11, 20)})
EAR = {(3, 12), (4, 13), (5, 13), (5, 14), (6, 14), (5, 15), (6, 15)}  # pointy ear
EAR_IN = {(5, 14), (6, 15)}
BLUSH = {(8, 16), (9, 16), (22, 16), (23, 16)}


def goggles(y0: int) -> tuple[Pixels, Pixels, Pixels, Pixels]:
    """Brass-rimmed round lenses (both sides); y0 is the top row of the rims."""
    ring = rows({y0: (10, 13), y0 + 1: (9, 14), y0 + 2: (9, 14),
                 y0 + 3: (9, 14), y0 + 4: (9, 14), y0 + 5: (10, 13)})
    lens = rows({y0 + 1: (11, 12), y0 + 2: (10, 13), y0 + 3: (10, 13),
                 y0 + 4: (11, 12)})
    strap = rect(6, y0 + 2, 25, y0 + 3)
    bridge = {(15, y0 + 2), (16, y0 + 2)}
    return ring | mirror(ring), lens | mirror(lens), strap, bridge


LENS_Y = 10  # goggles worn over the eyes
UP_Y = 3     # goggles pushed up onto the hair

# ---------- body ----------
BODY = rows({20: (11, 20)} | {y: (10, 21) for y in range(21, 26)})
JACKET_SHADE = {(10, y) for y in range(21, 26)} | {(21, y) for y in range(21, 26)}
COLLAR = {(11, 20), (12, 20), (19, 20), (20, 20)}
ZIP = {(15, y) for y in range(20, 25)}
BELT = rect(10, 25, 21, 25)
BUCKLE = {(15, 25), (16, 25)}
POUCHES = {(11, 25), (12, 25), (19, 25), (20, 25)}
LEGS = rect(11, 26, 14, 27) | rect(17, 26, 20, 27)
BOOTS = rows({28: (10, 14), 29: (9, 14)}) | rows({28: (17, 21), 29: (17, 22)})
BOOT_CUFF = rect(10, 28, 14, 28) | rect(17, 28, 21, 28)
ARM_L = rect(8, 21, 9, 22)  # screen-left arm reaching to the blaster grip
STARS = [{(2, 3), (1, 4), (2, 4), (3, 4), (2, 5)},
         {(29, 13), (28, 14), (29, 14), (30, 14), (29, 15)}]


def blaster(dy: int) -> dict[str, Pixels]:
    """Chunky toy blaster held across the body, pointing right."""
    s = lambda px: {(x, y + dy) for x, y in px}  # noqa: E731
    return {
        "body": s(rect(12, 21, 24, 23)),
        "barrel": s(rect(25, 21, 28, 22)),
        "under": s(rect(12, 23, 24, 23)),
        "stripe": s(rect(12, 21, 24, 21)),
        "window": s(rect(13, 22, 19, 22)),
        "meter": s({(13, 22), (15, 22), (17, 22), (19, 22)}),
        "muzzle": s({(28, 21), (28, 22)}),
        "grip": s({(13, 24), (14, 24), (13, 25)}),
        "glove_back": s(rect(10, 22, 11, 23) | {(9, 23)}),   # holding the stock
        "glove_front": s(rect(20, 23, 21, 24) | {(22, 24)}),  # supporting underneath
    }


def compose_goggles(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                    hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                    zzz: int = 0) -> Grid:
    """Goggles: flame-haired kid with aviator goggles and a toy blaster."""
    cv = Canvas()
    asleep = eyes == "sleep"
    goggles_up = hat_lift or eyes in ("happy", "sleep")

    # legs, boots and jacket
    cv.part(LEGS, "gg_coat_dk")
    cv.part(BOOTS, "gg_boot")
    cv.dots(BOOT_CUFF, "R")
    cv.part(BODY, "gg_coat")
    cv.dots(JACKET_SHADE, "gg_coat_dk")
    cv.dots(COLLAR | ZIP, "gg_trim")
    cv.dots(BELT, "gg_strap")
    cv.dots(POUCHES, "c")
    cv.dots(BUCKLE, "G")
    cv.part(ARM_L, "gg_coat")

    # head, ear and face
    cv.part(EAR, "O")
    cv.dots(EAR_IN, "P")
    cv.part(HEAD, "O")
    cv.dots(BLUSH, "P")

    # flame hair: alternate spikes flare up with a bright lick (idle cycle)
    phase = tail % 2
    licks: Pixels = set()
    if not asleep:
        for i in range(len(SPIKES)):
            if i % 2 == phase:
                licks |= flame_lick(i)
    cv.part(HAIR | licks, "gg_hair")
    cv.dots(BANGS, "gg_hair")
    cv.dots(HAIR_SHADE, "gg_hair_dk")
    if not asleep:
        for i in range(len(SPIKES)):
            cv.dots(spike_core(i) if i % 2 == phase else
                    set(sorted(spike_core(i), key=lambda p: p[1])[1:]), "gg_hair_lt")
        cv.dots(licks, "gg_hair_lt")

    # goggles: over the eyes, or pushed up onto the hair
    look, tilt = gaze_shift(eyes)
    ring, lens, strap, bridge = goggles(UP_Y if goggles_up else LENS_Y)
    cv.dots(strap, "gg_strap")
    cv.part(ring, "G")
    cv.dots(bridge, "G")
    cv.dots(lens, "L")
    if goggles_up:
        # bare eyes on the face: ^ ^ when happy, closed arcs when asleep
        if asleep:
            cv.dots({(10, 13), (11, 14), (12, 14), (13, 13),
                     (18, 13), (19, 14), (20, 14), (21, 13)}, "E")
        else:
            cv.dots({(10, 14), (11, 13), (12, 13), (13, 14),
                     (18, 14), (19, 13), (20, 13), (21, 14)}, "E")
            cv.dots({(11, UP_Y + 1), (19, UP_Y + 1)}, "w")  # lens shine
    else:
        y0 = LENS_Y
        if eyes == "blink":
            cv.dots({(x, y0 + 3) for x in (10, 11, 12, 13, 18, 19, 20, 21)}, "E")
        else:
            h = 3 if eyes == "wide" else 2
            top = (y0 + 2 if h == 2 else y0 + 1) + tilt
            cv.dots(rect(11 + look, top, 12 + look, top + h - 1)
                    | rect(19 + look, top, 20 + look, top + h - 1), "E")
            cv.dots({(11 + look, top), (19 + look, top)}, "w")
        # a glint sweeps along the top of the lenses
        gx = (10, 11, 12, 13)[tail % 4]
        cv.dots({(gx, y0 + 1), (gx + 8, y0 + 1)} - {(11 + look, y0 + 1),
                                                   (19 + look, y0 + 1)}, "w")

    # mouth: cheeky grin with buck teeth, or open while talking
    if mouth == "open":
        cv.dots(rect(14, 16, 17, 18) | {(13, 16), (18, 16)}, "M")
        cv.dots({(14, 16), (15, 16), (16, 16), (17, 16)}, "w")
        cv.dots({(15, 18), (16, 18)}, "T")
    elif asleep:
        cv.dots({(15, 17), (16, 17)}, "M")
    else:
        cv.dots({(13, 16), (14, 17), (15, 17), (16, 17), (17, 17), (18, 16)}, "K")
        cv.dots({(15, 16), (16, 16)}, "w")

    # blaster; its charge meter fills while thinking
    b = blaster(0)
    cv.part(b["grip"], "D")
    cv.part(b["body"] | b["barrel"], "gg_blast")
    cv.dots(b["under"], "gg_blast_dk")
    cv.dots(b["stripe"], "gg_trim")
    cv.dots(b["window"] | b["muzzle"], "D")
    meter = sorted(b["meter"])
    lit = (busy % 4) + 1 if busy is not None else (0 if asleep else 1)
    cv.dots(set(meter[:lit]), "gg_charge")
    if busy is not None and busy % 4 >= 2:  # muzzle sparkle
        my = 21
        spark = {(29, my), (29, my + 1)}
        if busy % 4 == 3:
            spark |= {(30, my - 1), (30, my + 2), (31, my), (31, my + 1)}
        cv.dots(spark, "gg_charge")
    cv.part(b["glove_back"], "gg_glove")
    cv.part(b["glove_front"], "gg_glove")

    if hat_lift:  # celebration sparkles
        for star in STARS:
            cv.dots(star, "y")
    return finish_overlays(cv, sound, zzz)
