"""関数が参照するデータ (文字列や定数) を洗い出して中身を見る。"""
import struct
import sys
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

sys.path.insert(0, r'C:\Users\aoiro\AppData\Local\Temp\opencode')
from arm_dis import Elf

elf = Elf()
name = sys.argv[1]
sym = elf.sym(name)
value = sym['st_value']
addr = value & ~1
size = sym['st_size']
raw = elf.read(addr, size)

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
insns = list(md.disasm(raw, addr))

refs = []
for idx, insn in enumerate(insns):
    if insn.mnemonic.startswith("ldr") and "[pc" in insn.op_str:
        reg = insn.op_str.split(",")[0].strip()
        imm = int(insn.op_str.split("#")[1].split("]")[0].strip("+-"), 0)
        literal = ((insn.address + 4) & ~3) + imm
        loaded = struct.unpack("<I", elf.read(literal, 4))[0]
        target = elf.resolve(literal)
        for nxt in insns[idx + 1:idx + 6]:
            if nxt.mnemonic.startswith("add") and reg in nxt.op_str and "pc" in nxt.op_str:
                target = elf.resolve(target)
                break
        refs.append((insn.address, target))

seen = set()
for at, target in refs:
    if target in seen:
        continue
    seen.add(target)
    section = elf.section_of(target)
    if section is None:
        continue
    s = elf.cstring(target, 200)
    if s and len(s) >= 3 and all(32 <= ord(c) < 127 or ord(c) > 160 for c in s):
        print(f"0x{at:08x} -> 0x{target:x} [{section}] 文字列: {s[:120]}")
    else:
        raw16 = elf.read(target, 16)
        print(f"0x{at:08x} -> 0x{target:x} [{section}] データ: {raw16.hex()}")
