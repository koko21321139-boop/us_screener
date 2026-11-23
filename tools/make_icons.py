"""アイコン画像を作る（Pillow が必要。1 回だけ実行すればよい）"""
from PIL import Image, ImageDraw
def icon(size, pad):
    im = Image.new("RGB", (size, size), "#0f4c5c"); d = ImageDraw.Draw(im)
    s = size * (1 - 2 * pad); o = size * pad; w = max(3, int(size / 28))
    pts = [(0.05, .80), (.25, .62), (.40, .70), (.58, .45), (.70, .52), (.95, .18)]
    d.line([(o + x * s, o + y * s) for x, y in pts], fill="#f2c14e", width=w, joint="curve")
    pts2 = [(0.05, .88), (.35, .78), (.65, .62), (.95, .40)]
    d.line([(o + x * s, o + y * s) for x, y in pts2], fill="#a9bce0", width=max(2, w // 2), joint="curve")
    return im
icon(192, .14).save("icons/icon-192.png"); icon(512, .14).save("icons/icon-512.png")
icon(512, .24).save("icons/maskable-512.png"); icon(180, .14).save("icons/apple-touch-icon.png")
