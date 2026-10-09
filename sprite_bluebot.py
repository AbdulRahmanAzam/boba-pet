"""Blue Bot: a chibi robot with a big helmet, glowing visor eyes and a toy blaster.

Same layered approach as sprites.py: filled parts get an automatic dark
outline, details are painted on top with dots().
"""

from sprites import SOUND, Canvas, Grid, Pixels, finish_overlays, gaze_shift, mirror, rect, rows

NAME = "Blue Bot"  # label in the Character menu

EXTRA_PALETTE: dict[str, str] = {
    "bb_helm": "#9CC2F7",   # helmet light blue
    "bb_helm2": "#6A93DE",  # helmet shade / crease
    "bb_shine": "#E8F3FF",  # helmet highlight
    "bb_visor": "#1B2132",  # dark visor band
    "bb_eye": "#62F27A",    # glowing eye
    "bb_eye2": "#2DAE4C",   # eye scanline
    "bb_off": "#2C4A36",    # eye / light switched off
    "bb_body": "#7AA4EC",   # body blue
    "bb_teal": "#3FCDB8",   # hands, boots, blaster
    "bb_teal2": "#23917F",  # teal shade
    "bb_metal": "#3D4454",  # blaster dark parts
    "bb_glow": "#C9FFD6",   # charge glow core
}

# ---------- shapes ----------
HELMET = rows({2: (12, 20), 3: (10, 22), 4: (9, 23), 5: (8, 24), 6: (8, 24)}
              | {y: (7, 25) for y in range(7, 16)}
              | {16: (8, 24), 17: (9, 23), 18: (11, 21)})
HELMET_SHADE = {(24, y) for y in range(9, 16)} | rect(10, 17, 22, 17) | {(23, 16)}
HELMET_SHINE = {(12, 3), (13, 3), (10, 4), (11, 4), (9, 5), (9, 6)}
HELMET_VENTS = {(20, 4), (21, 4), (21, 5), (22, 5)}
CREASE = rect(8, 8, 24, 8)
VISOR = rows({9: (10, 25), 10: (9, 25), 11: (9, 25), 12: (9, 25),
              13: (9, 25), 14: (10, 25)})
BLUSH = {(10, 16), (11, 16), (21, 16), (22, 16)}

EAR_RING = rows({10: (5, 7), 11: (4, 8), 12: (4, 8), 13: (4, 8), 14: (5, 7)})
EAR_LIGHT = rows({11: (5, 7), 12: (5, 7), 13: (5, 7)})


def fin(wiggle: int) -> Pixels:
    """Slanted side fin rising up-left from the ear; the tip sways by `wiggle`."""
    base = rows({7: (5, 8), 8: (6, 9), 9: (6, 9)})
    mid = rows({5: (4, 7), 6: (4, 8)})
    tip = rows({2: (2, 4), 3: (2, 5), 4: (3, 6)})
    return base | mid | {(x + wiggle, y) for x, y in tip}


BODY = rows({19: (12, 20)} | {y: (11, 21) for y in range(20, 26)})
BELT = rect(11, 25, 21, 25)
LEGS = rect(12, 26, 13, 27) | rect(19, 26, 20, 27)
BOOTS = rows({28: (10, 14), 29: (10, 14)}) | rows({28: (18, 22), 29: (18, 22)})
BOOT_SHADE = {(10, 29), (11, 29), (12, 29), (13, 29), (14, 29),
              (18, 29), (19, 29), (20, 29), (21, 29), (22, 29)}

# Chunky toy blaster held across the belly, pointing right: dark frame,
# teal top rail, grip and magazine below, charge cells along the side.
GUN = rect(12, 21, 25, 22)
GUN_RAIL = rect(13, 21, 24, 21)
GUN_SIGHT = rect(20, 20, 21, 20)
GUN_MUZZLE = rect(26, 21, 27, 21)
GUN_GRIP = rect(14, 23, 15, 24)
GUN_MAG = rect(20, 23, 20, 24)
CHARGE_CELLS = [(15, 22), (17, 22), (19, 22), (21, 22)]
HAND_BACK = rect(13, 23, 14, 24)
HAND_FRONT = rect(22, 22, 24, 23)
# muzzle glow, one ring added per thinking frame
GLOW = [{(28, 21)},
        {(29, 21), (28, 20), (28, 22)},
        {(30, 21), (29, 20), (29, 22)},
        {(31, 21), (30, 20), (30, 22), (29, 19), (29, 23)}]

# Raised blaster for the celebrate pose (pointing straight up on the right).
GUN_UP = rows({y: (27, 29) for y in range(9, 21)})
GUN_UP_STRIPE = rect(29, 9, 29, 20)
GUN_UP_MUZZLE = rect(28, 6, 28, 8)
GUN_UP_SIGHT = rect(25, 13, 26, 14)
HAND_UP = rect(25, 18, 27, 20)
ARM_UP = rect(22, 19, 24, 20)
HAND_SIDE = rect(9, 22, 10, 23)
SPARKLES = {(31, 4), (30, 3), (31, 2), (25, 5), (24, 4)}

MOUTH_SMILE = {(14, 16), (15, 17), (16, 17), (17, 16)}
GRILLE = rect(14, 16, 17, 17)


def eye_shapes(eyes: str) -> tuple[Pixels, Pixels]:
    """Return (bright pixels, scanline pixels) for both visor eyes."""
    look, tilt = gaze_shift(eyes)
    lx, rx = 11 + look, 18 + look
    if eyes in ("blink", "sleep"):
        line = rect(lx, 12, lx + 4, 12) | rect(rx, 12, rx + 4, 12)
        return line, set()
    if eyes == "happy":
        arc = lambda x: {(x, 12), (x + 1, 11), (x + 2, 11), (x + 3, 11), (x + 4, 12)}  # noqa: E731
        return arc(lx) | arc(rx), set()
    top, bot = (9, 14) if eyes == "wide" else (10 + tilt, 13 + tilt)

    def disc(x: int) -> Pixels:
        px = rect(x, top, x + 4, bot) - {(x, top), (x + 4, top), (x, bot), (x + 4, bot)}
        return px

    shape = disc(lx) | disc(rx)
    scan = {(x, y) for x, y in shape if y == bot - 1}  # one scanline per eye
    return shape - scan, scan


def compose_bluebot(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                    hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                    zzz: int = 0) -> Grid:
    """Blue Bot: helmet-head robot whose blaster charges up while thinking."""
    cv = Canvas()
    asleep = eyes == "sleep"
    up = -hat_lift  # head bounces up 1px when celebrating
    lift = lambda px: {(x, y + up) for x, y in px}  # noqa: E731

    # boots and legs
    cv.part(LEGS, "bb_body")
    cv.part(BOOTS, "bb_teal")
    cv.dots(BOOT_SHADE, "bb_teal2")
    # slim body with belt and a chest light
    cv.part(BODY, "bb_body")
    cv.dots(BELT, "bb_helm2")
    cv.dots({(16, 24)}, "bb_teal")

    # side fin (behind the head), wiggling through the idle cycle
    wiggle = 0 if asleep else (0, -1, 0, 1)[tail % 4]
    cv.part(lift(fin(wiggle)), "bb_helm2")
    # helmet head
    cv.part(lift(HELMET), "bb_helm")
    cv.dots(lift(HELMET_SHADE), "bb_helm2")
    cv.dots(lift(HELMET_SHINE), "bb_shine")
    cv.dots(lift(HELMET_VENTS), "bb_helm2")
    cv.dots(lift(CREASE), "bb_helm2")
    cv.dots(lift(VISOR), "bb_visor")
    # glowing eyes on the visor
    bright, scan = eye_shapes(eyes)
    cv.dots(lift(bright), "bb_off" if asleep else "bb_eye")
    cv.dots(lift(scan), "bb_eye2")
    if not asleep and eyes not in ("blink", "happy"):  # tiny reflection
        cv.dots(lift({min(bright, key=lambda p: (p[1], p[0]))}), "bb_glow")
    cv.dots(lift(BLUSH), "P")
    # LED mouth: smile, or a grille that flickers while speaking
    if mouth == "open":
        cv.dots(lift(GRILLE), "bb_visor")
        lit = ({(14, 16), (16, 16), (15, 17), (17, 17)} if tail % 2 == 0
               else {(15, 16), (16, 16), (14, 17), (17, 17)})
        cv.dots(lift(lit), "bb_eye")
    else:
        cv.dots(lift(MOUTH_SMILE), "K")
    # round ear light: blinks in idle, pulses while listening, off when asleep
    cv.part(lift(EAR_RING), "bb_shine")
    if asleep:
        ear = "bb_off"
    elif sound:
        ear = "bb_glow" if tail % 2 else "bb_eye"
    else:
        ear = "bb_eye" if tail % 4 != 3 else "bb_eye2"
    cv.dots(lift(EAR_LIGHT), ear)
    cv.dots(lift({(6, 12)}), "bb_glow" if not asleep else "bb_off")

    if hat_lift:
        # celebrate: blaster raised high, sparkles from the muzzle
        cv.part(ARM_UP, "bb_body")
        cv.part(GUN_UP, "bb_teal")
        cv.dots(GUN_UP_STRIPE, "bb_teal2")
        cv.part(GUN_UP_SIGHT, "bb_metal")
        cv.part(GUN_UP_MUZZLE, "bb_metal")
        cv.part(HAND_UP, "bb_teal")
        cv.dots(SPARKLES, "y")
        cv.part(HAND_SIDE, "bb_teal")
    else:
        cv.part(GUN_GRIP | GUN_MAG, "bb_metal")
        cv.part(GUN_SIGHT | GUN_MUZZLE, "bb_metal")
        cv.part(GUN, "bb_metal")
        cv.dots(GUN_RAIL, "bb_teal2")
        # charge cells: fill up while thinking
        charged = (busy % 4) + 1 if busy is not None else 0
        for i, cell in enumerate(CHARGE_CELLS):
            cv.dots({cell}, "bb_eye" if i < charged else "bb_teal")
        if busy is not None:  # muzzle glow grows with the charge
            r = busy % 4
            for ring in GLOW[1:r + 1]:
                cv.dots(ring, "bb_eye")
            cv.dots(GLOW[0] | (GLOW[1] if r >= 2 else set()), "bb_glow")
        cv.part(HAND_BACK, "bb_teal")
        cv.part(HAND_FRONT, "bb_teal")

    # listening arcs go on the right; the left side holds the fin and ear
    for i in range(sound):
        cv.dots(mirror(SOUND[i]), "A")
    return finish_overlays(cv, 0, zzz)
