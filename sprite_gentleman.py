"""Gentleman: a jolly mustachioed fellow in a checkered cap with a toy blaster.

Original chibi interpretation (32x32), built with the same layered Canvas as
sprites.py: every part gets an auto outline, later parts paint over earlier.
"""

from sprites import Canvas, Grid, Pixels, finish_overlays, gaze_shift, mirror, rect, rows

NAME = "Gentleman"  # label in the Character menu

EXTRA_PALETTE: dict[str, str] = {
    "gm_cap": "#F7C335",    # cap yellow
    "gm_cap2": "#EE8F2B",   # cap orange checks / bill
    "gm_skin": "#FCD6B4",   # face and hands
    "gm_nose": "#F4A98C",   # big round nose
    "gm_hair": "#80502C",   # sideburns / mustache
    "gm_hair2": "#5C3620",  # mustache shade
    "gm_rim": "#4A2E1C",    # glasses frame
    "gm_lens": "#EAF6FF",   # lens glass / shoe shine
    "gm_shirt": "#3F7FDB",  # shirt and shoes
    "gm_shirt2": "#2B5CAE",  # shirt shade
    "gm_over": "#F4AE3A",   # overall
    "gm_over2": "#D98A26",  # overall shade / straps
    "gm_gun": "#59606E",    # blaster body
    "gm_gun2": "#8F99A8",   # blaster highlight
}

# ---------- shapes ----------
HEAD = rows({9: (8, 23), 10: (7, 24)} | {y: (6, 25) for y in range(11, 18)}
            | {18: (7, 24), 19: (8, 23), 20: (10, 21)})
EARS = {(5, 13), (5, 14), (26, 13), (26, 14)}
SIDEBURN_L = rows({10: (7, 10), 11: (6, 8), 12: (6, 7), 13: (6, 6), 14: (6, 6),
                   15: (6, 6)})
BLUSH = {(7, 16), (8, 16), (23, 16), (24, 16)}

# round glasses: a 5x5 rim with a 3x3 lens inside each
RIM_L = {(10, 11), (11, 11), (12, 11), (9, 12), (9, 13), (9, 14), (13, 12), (13, 13),
         (13, 14), (10, 15), (11, 15), (12, 15)}
LENS_L = rect(10, 12, 12, 14)
BRIDGE = {(14, 12), (15, 11), (16, 11), (17, 12)}
TEMPLES = {(8, 12), (23, 12)}

NOSE = rows({14: (15, 16), 15: (14, 17), 16: (14, 17), 17: (15, 16)})
NOSE_SHINE = {(15, 15)}

# bushy mustache with curled tips that droop (or flick up)
MUSTACHE = rows({17: (11, 20), 18: (9, 22)}) | {(9, 19), (10, 19), (11, 19),
                                                 (20, 19), (21, 19), (22, 19)}
TIPS_DOWN = {(8, 19), (8, 20), (23, 19), (23, 20)}
TIPS_UP = {(8, 17), (7, 16), (23, 17), (24, 16)}
MUSTACHE_SHADE = {(10, 19), (21, 19), (12, 18), (19, 18)}

# body: blue shirt under a yellow-orange overall
SHIRT = rows({21: (11, 20)} | {y: (10, 21) for y in range(22, 27)})
OVERALL = rect(12, 22, 19, 26) | rect(11, 27, 14, 28) | rect(17, 27, 20, 28)
OVERALL_SHADE = {(12, 26), (19, 26), (14, 28), (20, 28)}
LEG_GAP = {(15, 27), (16, 27), (15, 28), (16, 28)}
STRAPS = {(12, 21), (19, 21)}
BUTTONS = {(13, 22), (18, 22)}
POCKET = rect(14, 24, 17, 24)
SHOES = rows({29: (10, 14), 30: (9, 14)}) | rows({29: (17, 21), 30: (17, 22)})
SLEEVE_L = rect(7, 21, 9, 23)
HAND_L = rect(7, 24, 9, 25)
SLEEVE_R = rect(21, 21, 23, 23)

# chunky toy blaster held at the right side, muzzle pointing right
GUN_BODY = rect(22, 22, 27, 24)
GUN_BARREL = rect(28, 22, 29, 23)
GUN_SHINE = rect(23, 22, 27, 22)
COILS = [(23, 23), (24, 23), (25, 23), (26, 23)]
MUZZLE = {(30, 22), (30, 23)}
HAND_R = rect(23, 25, 25, 26)

# cap: rounded crown with orange checks, white oval badge, bill to the right
CAP_CROWN = rows({1: (11, 20), 2: (9, 22), 3: (8, 23), 4: (7, 24), 5: (7, 24),
                  6: (7, 24), 7: (6, 25), 8: (6, 25)})
CAP_BRIM = rows({9: (6, 28)})
CAP_CHECKS = {(x, y) for x, y in CAP_CROWN if (x // 2 + y // 2) % 2 == 0}
BADGE = rows({3: (14, 17), 4: (13, 18), 5: (13, 18), 6: (14, 17)})
CAP_SHINE = {(10, 2), (9, 3), (8, 4)}
HAIR_TOP = rows({6: (10, 16), 7: (8, 20), 8: (7, 23), 9: (7, 24)})


def compose_gentleman(eyes: str = "open", mouth: str = "closed", tail: int = 0,
                      hat_lift: int = 0, busy: int | None = None, sound: int = 0,
                      zzz: int = 0) -> Grid:
    """Gentleman: checkered cap, round glasses, bushy mustache, toy blaster."""
    cv = Canvas()
    asleep = eyes == "sleep"

    # shoes, shirt with the overall, left arm
    cv.part(SHOES, "gm_shirt")
    cv.dots({(10, 29), (18, 29)}, "gm_lens")
    cv.part(SHIRT | OVERALL, "gm_shirt")
    cv.dots({(10, 25), (10, 26), (21, 25), (21, 26)}, "gm_shirt2")
    cv.dots(OVERALL, "gm_over")
    cv.dots(OVERALL_SHADE | STRAPS | POCKET, "gm_over2")
    cv.dots(LEG_GAP, "K")
    cv.dots(BUTTONS, "G")
    cv.part(SLEEVE_L, "gm_shirt")
    cv.part(HAND_L, "gm_skin")

    # blaster; coils light up one by one while thinking (charging)
    cv.part(SLEEVE_R, "gm_shirt")
    cv.part(GUN_BARREL | GUN_BODY, "gm_gun")
    cv.dots(GUN_SHINE | {(28, 22), (29, 22)}, "gm_gun2")
    charge = (busy % 4) + 1 if busy is not None else 0
    for i, c in enumerate(COILS):
        cv.dots({c}, "y" if i < charge else "D")
    cv.part(MUZZLE, "A" if charge == 4 else "B")
    if charge == 4:
        cv.dots({(29, 20), (31, 20), (31, 25)}, "y")  # charged sparkle
    cv.part(HAND_R, "gm_skin")

    # head, ears, sideburns
    cv.part(HEAD | EARS, "gm_skin")
    cv.dots({(5, 13), (26, 14)}, "gm_nose")
    cv.dots(SIDEBURN_L | mirror(SIDEBURN_L), "gm_hair")
    cv.dots(BLUSH, "P")

    # eyes behind round glasses
    cv.dots(LENS_L | mirror(LENS_L), "gm_lens")
    look, tilt = gaze_shift(eyes)
    for ex in (11, 20):
        if eyes == "blink":
            cv.dots({(ex - 1, 14), (ex, 14), (ex + 1, 14)}, "E")
        elif eyes == "happy":
            cv.dots({(ex - 1, 14), (ex, 13), (ex + 1, 14)}, "E")
        elif asleep:
            cv.dots({(ex - 1, 13), (ex, 14), (ex + 1, 13)}, "E")
        elif eyes == "wide":  # bright white lenses with tiny alert pupils
            cv.dots(rect(ex - 1, 12, ex + 1, 14), "w")
            cv.dots({(ex, 13)}, "E")
        else:
            cv.dots({(ex + look, 13 + tilt), (ex + look, 14 + tilt)}, "E")
    cv.dots(RIM_L | mirror(RIM_L) | BRIDGE | TEMPLES, "gm_rim")
    # glint: idle frames flash the lenses; thinking sweeps it across both
    if busy is not None:
        gx = (10, 12, 19, 21)[busy % 4]
        cv.dots({(gx, 12)}, "w")
    elif not asleep and tail % 2 == 1:
        cv.dots({(10, 12), (19, 12)}, "w")

    # mouth under the mustache; the mustache lifts while talking
    lift = 1 if mouth == "open" else 0
    up = lambda px: {(x, y - lift) for x, y in px}  # noqa: E731
    if mouth == "open":
        cv.part(rect(14, 19, 17, 20), "M")
        cv.dots({(15, 20), (16, 20)}, "T")
    elif not asleep:
        cv.dots({(15, 20), (16, 20)}, "M")
    # tips twitch up on idle frame 2 and when happy
    tips = TIPS_UP if (tail % 4 == 2 or eyes == "happy") else TIPS_DOWN
    cv.part(up(MUSTACHE | tips), "gm_hair")
    cv.dots(up(MUSTACHE_SHADE), "gm_hair2")
    cv.part(NOSE, "gm_nose")
    cv.dots(NOSE_SHINE, "w")

    # cap (doffed up and tipped right when celebrating, showing his hair)
    dx, dy = hat_lift, -3 * hat_lift
    sh = lambda px: {(x + dx, y + dy) for x, y in px}  # noqa: E731
    if hat_lift:
        cv.part(HAIR_TOP, "gm_hair")
        cv.dots({(10, 7), (12, 8), (15, 8), (18, 9)}, "gm_hair2")
    cv.part(sh(CAP_CROWN | CAP_BRIM), "gm_cap")
    cv.dots(sh(CAP_CHECKS), "gm_cap2")
    cv.dots(sh(CAP_BRIM), "gm_cap2")
    cv.dots(sh(CAP_SHINE), "w")
    cv.part(sh(BADGE), "w")
    cv.dots(sh({(14, 4), (15, 4)}), "gm_lens")
    if hat_lift:  # the doffed crown pokes past the top edge: close it with outline
        cv.dots({(x, 0) for x in range(32) if cv.grid[0][x] not in (None, "K")}, "K")
    return finish_overlays(cv, sound, zzz)
