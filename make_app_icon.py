# -*- coding: utf-8 -*-
"""生成应用图标 app.ico —— A股配色（红涨绿跌）：三根上升红色K线 + 金色趋势线，深色圆角底。

重复生成：python make_app_icon.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "app.ico")

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 深色圆角背景
d.rounded_rectangle([6, 6, S - 6, S - 6], radius=52, fill=(23, 27, 38, 255))

RED = (235, 72, 60, 255)
GOLD = (255, 205, 88, 255)

# 三根依次升高的红色K线: (x, 上影顶, 实体顶, 实体底, 下影底)
CANDLES = [
    (74, 128, 150, 210, 232),
    (128, 88, 110, 176, 198),
    (182, 42, 66, 138, 162),
]
BW = 30  # 实体宽
for x, wick_t, body_t, body_b, wick_b in CANDLES:
    d.line([(x, wick_t), (x, wick_b)], fill=RED, width=10)
    d.rectangle([x - BW // 2, body_t, x + BW // 2, body_b], fill=RED)

# 金色上升趋势线 + 箭头
d.line([(44, 120), (98, 86), (138, 98), (212, 36)], fill=GOLD, width=11, joint="curve")
d.polygon([(212, 36), (195, 72), (174, 46)], fill=GOLD)

img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("saved:", OUT)
