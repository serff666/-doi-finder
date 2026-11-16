"""Draws the DOI Finder icon as vector geometry, natively at every size (crisp at 16 px too)."""
import sys
from PIL import Image, ImageDraw, ImageChops

def lerp(a, b, t): return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def render(size, ss=4):
    S = size * ss
    small = size <= 32
    m = 0.02 if small else 0.045                    # margin
    r = 0.20 if small else 0.225                    # corner radius
    # --- background: diagonal indigo -> violet gradient, rounded square
    c1, c2 = (74, 98, 255), (130, 60, 230)
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(c1, c2, (x + y) / (2 * S - 2))
    bg = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bg).rounded_rectangle([m*S, m*S, (1-m)*S, (1-m)*S], radius=r*S, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0)); img.paste(grad, (0, 0), bg)
    if not small:                                   # soft top highlight
        hl = Image.new("L", (S, S), 0)
        ImageDraw.Draw(hl).ellipse([-0.2*S, -0.9*S, 1.2*S, 0.45*S], fill=46)
        from PIL import ImageFilter
        hl = hl.filter(ImageFilter.GaussianBlur(0.03*S))
        hl = ImageChops.multiply(hl, bg)
        img.paste((255, 255, 255), (0, 0), hl)
    # --- the "D": straight stem + half-ellipse bowl (geometric, bold)
    if small:  sx, top, bot, cx, rx, st, bw = 0.24, 0.20, 0.80, 0.46, 0.34, 0.17, 0.16
    else:      sx, top, bot, cx, rx, st, bw = 0.275, 0.235, 0.765, 0.465, 0.295, 0.135, 0.125
    cy = (top + bot) / 2; ry = (bot - top) / 2
    def shape(left, t, b, rxx, ryy, fill):
        """rect from `left` to cx, plus the RIGHT half of an ellipse centred on (cx, cy)."""
        m = Image.new("L", (S, S), 0); dd = ImageDraw.Draw(m)
        dd.ellipse([(cx-rxx)*S, (cy-ryy)*S, (cx+rxx)*S, (cy+ryy)*S], fill=255)
        dd.rectangle([0, 0, cx*S, S], fill=0)                      # keep only the right half
        dd.rectangle([left*S, t*S, cx*S, b*S], fill=255)
        return m
    outer = shape(sx, top, bot, rx, ry, 255)
    counter = shape(sx+st, cy-(ry-bw), cy+(ry-bw), rx-bw, ry-bw, 255)
    mk = ImageChops.subtract(outer, counter)
    sh = Image.new("L", (S, S), 0)                              # faint drop shadow
    sh.paste(mk, (0, int(0.012*S)))
    if not small:
        from PIL import ImageFilter
        sh = sh.filter(ImageFilter.GaussianBlur(0.012*S)).point(lambda v: int(v*0.35))
        img.paste((30, 20, 110), (0, 0), ImageChops.multiply(sh, bg))
    img.paste((255, 255, 255), (0, 0), mk)
    # --- download-tray bar under the D (large sizes only)
    if not small:
        ImageDraw.Draw(img).rounded_rectangle([0.34*S, 0.815*S, 0.66*S, 0.855*S], radius=0.02*S, fill=(255, 255, 255, 205))
    return img.resize((size, size), Image.LANCZOS)

if __name__ == "__main__":
    out = sys.argv[1]
    big = render(1024, ss=2); big.save(f"{out}/icon.png")
    big.resize((64, 64), Image.LANCZOS).save(f"{out}/favicon.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [render(s) for s in sizes]
    imgs[-1].save(f"{out}/icon.ico", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    imgs[-1].save(f"{out}/favicon.ico", sizes=[(s, s) for s in (16, 32, 48)], append_images=[imgs[0], imgs[3], imgs[5]][:2])
