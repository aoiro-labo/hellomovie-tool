"""rodata から鍵候補 (base64/hex 風の定数) を抜き出す。"""
import re
import sys
from pathlib import Path
sys.path.insert(0, r'C:\Users\aoiro\AppData\Local\Temp\opencode')
from arm_dis import Elf

elf = Elf()
vaddr, off, size = elf.sections['.rodata']
data = elf.data[off:off + size]

B64 = re.compile(rb'^[A-Za-z0-9+/=_-]{12,}$')
HEX = re.compile(rb'^[0-9a-fA-F]{16,}$')

cands = []
i = 0
while i < len(data):
    j = data.find(b'\x00', i)
    if j < 0:
        break
    blob = data[i:j]
    if len(blob) >= 12:
        if B64.match(blob) or HEX.match(blob):
            cands.append((vaddr + i, blob))
    i = j + 1

print(f"候補 {len(cands)} 個")
for addr, blob in cands:
    print(f"0x{addr:08x}  len={len(blob):3d}  {blob[:80].decode('ascii')}")
