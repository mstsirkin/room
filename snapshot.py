#!/usr/bin/env python3
"""Render a captured room session as a terminal screenshot

vhs would be the obvious tool, but it needs ttyd. This takes the real bytes of
a session and draws them, so the picture says exactly what the terminal did.

    room who > session.txt && cat room.log >> session.txt
    ./snapshot.py session.txt demo.png
"""

import re
import sys

from PIL import Image, ImageDraw, ImageFont


FONTS = [
    '/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf',
    '/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf',
    '/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf',
    '/usr/share/fonts/urw-base35/NimbusMonoPS-Regular.otf',
]

BACKGROUND = '#12131a'
PLAIN = '#d8dce6'
PROMPT = '#5af78e'
COMMAND = '#ffffff'
DIM = '#6b7080'
NICK = '#58d7f0'
MENTION = '#ffd479'
ALIVE = '#5af78e'

SIZE = 15
COLUMNS = 98
MARGIN = 22


def load_font():
    for path in FONTS:
        try:
            return ImageFont.truetype(path, SIZE)
        except OSError:
            continue
    raise SystemExit('no monospace font found')


def wrap(text, width, indent):
    """Wrap like a terminal would, but indent the continuations to stay read

    A terminal wraps hard at the edge. Hanging the continuation under the
    message keeps the nick column legible, which is the point of the picture.
    """
    lines = []
    while len(text) > width:
        cut = text.rfind(' ', 0, width)
        if cut <= indent:
            cut = width
        lines.append(text[:cut])
        text = ' ' * indent + text[cut:].lstrip()
        width = COLUMNS
    lines.append(text)
    return lines


def spans(line):
    """Split one line into coloured pieces"""
    if line.startswith('$ '):
        return [('$ ', PROMPT), (line[2:], COMMAND)]

    if line.startswith('* '):
        return [(line, DIM)]

    if line.startswith('--'):
        return [(line, DIM)]

    seq = re.match(r'^(\[\d+\] )(<[^>]+>)( ?)(.*)$', line)
    if seq:
        out = [(seq.group(1), DIM), (seq.group(2), NICK), (seq.group(3), PLAIN)]
        return out + body(seq.group(4))

    roster = re.match(r'^(\S+)(\s+)(alive|human|left|unknown|gone|exited\(\d+\))(\s+)(.*)$',
                      line)
    if roster:
        state = ALIVE if roster.group(3) in ('alive', 'human') else DIM
        return [
            (roster.group(1), NICK),
            (roster.group(2), PLAIN),
            (roster.group(3), state),
            (roster.group(4), PLAIN),
            (roster.group(5), DIM),
        ]

    if line.startswith('you are:'):
        return [('you are:', DIM), (line[8:], NICK)]

    return body(line)


def body(text):
    """Pick out @mentions, which is how agents address each other"""
    out = []
    for piece in re.split(r'(@\w+)', text):
        if piece:
            out.append((piece, MENTION if piece.startswith('@') else PLAIN))
    return out


def main():
    source, target = sys.argv[1], sys.argv[2]
    font = load_font()

    raw = open(source).read().rstrip('\n').split('\n')
    lines = []
    for line in raw:
        indent = 7 if line.startswith('[') else 0
        lines.extend(wrap(line, COLUMNS, indent) if len(line) > COLUMNS else [line])

    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    advance = probe.textlength('M' * 100, font=font) / 100
    height = SIZE + 7

    image = Image.new(
        'RGB',
        (int(advance * COLUMNS) + MARGIN * 2, height * len(lines) + MARGIN * 2),
        BACKGROUND,
    )
    draw = ImageDraw.Draw(image)

    for row, line in enumerate(lines):
        x = MARGIN
        for text, colour in spans(line):
            draw.text((x, MARGIN + row * height), text, font=font, fill=colour)
            x += advance * len(text)

    image.save(target)
    print(f'{target}: {image.width}x{image.height}, {len(lines)} lines')


if __name__ == '__main__':
    main()
