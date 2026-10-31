"""Сборка макетов «Встречи»: python3 -I _build.py (из этой папки или с путём)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _p1 import p01, p02, p03
from _p2 import p04
from _p3 import pB, pC
from _p5 import pA
from _p4 import p06, p07, index

OUT = {'index.html': index, '01-section.html': p01, '02-responsibility.html': p02, '03-offer.html': p03,
       '04-recording.html': p04, '05-meeting-A.html': pA, '05-meeting-B.html': pB, '05-meeting-C.html': pC,
       '06-meeting-states.html': p06, '07-templates.html': p07}
for name, fn in OUT.items():
    with open(os.path.join(HERE, name), 'w', encoding='utf-8') as f:
        f.write(fn())
    print(name)
