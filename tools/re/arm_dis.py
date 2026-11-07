"""ARM (Thumb) 逆アセンブルヘルパ + 文字列参照の解決。"""
import struct
import sys
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from elftools.elf.elffile import ELFFile

sys.stdout.reconfigure(encoding="utf-8")

SO = Path(r"C:\Users\aoiro\Documents\hellomovie-tool\analysis\native\armeabi-v7a\libsapkit.so")


class Elf:
    def __init__(self, path=SO):
        self.path = Path(path)
        self.fh = open(path, "rb")
        self.elf = ELFFile(self.fh)
        self.data = self.path.read_bytes()
        self.sections = {}
        for s in self.elf.iter_sections():
            self.sections[s.name] = (s["sh_addr"], s["sh_offset"], s["sh_size"])
        self.relocs = {}
        for sec in self.elf.iter_sections():
            if not hasattr(sec, "iter_relocations"):
                continue
            sec_data = sec.data() if sec["sh_type"] != "SHT_NOBITS" else b""
            for rel in sec.iter_relocations():
                addend = rel.entry.get("r_addend")
                if addend is None and sec["sh_type"] == "SHT_REL":
                    # 32bit ARM の REL は addend が対象のセクション内に格納されている
                    raw = self.read(rel["r_offset"], 4)
                    if len(raw) == 4:
                        addend = struct.unpack("<I", raw)[0]
                if addend is None:
                    continue
                self.relocs[rel["r_offset"]] = addend

    def resolve(self, addr: int) -> int:
        """GOT アドレスから実アドレスを解決します (PREL 対応)。"""
        return self.relocs.get(addr, addr)

    def sym(self, name):
        for secname in (".dynsym", ".symtab"):
            sec = self.elf.get_section_by_name(secname)
            if sec is None:
                continue
            for s in sec.iter_symbols():
                if s.name == name:
                    return s
        return None

    def addr_to_offset(self, addr):
        for name, (vaddr, off, size) in self.sections.items():
            if vaddr <= addr < vaddr + size:
                return off + (addr - vaddr)
        return None

    def read(self, addr, n):
        off = self.addr_to_offset(addr)
        if off is None:
            return b""
        return self.data[off:off + n]

    def cstring(self, addr, limit=120):
        raw = self.read(addr, limit)
        end = raw.find(b"\x00")
        if end < 0:
            return None
        try:
            return raw[:end].decode("utf-8")
        except UnicodeDecodeError:
            return None

    def section_of(self, addr):
        for name, (vaddr, off, size) in self.sections.items():
            if vaddr <= addr < vaddr + size:
                return name
        return None


def disasm(elf, name, max_insns=400):
    sym = elf.sym(name)
    if sym is None:
        print(f"シンボルが見つかりません: {name}")
        return
    value = sym["st_value"]
    thumb = bool(value & 1)
    addr = value & ~1
    size = sym["st_size"]
    raw = elf.read(addr, size)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    insns = list(md.disasm(raw, addr))
    print(f"=== {name} @ 0x{addr:x} (size {size}, {'Thumb' if thumb else 'ARM'}) ===")
    for idx, insn in enumerate(insns):
        extra = ""
        if insn.mnemonic.startswith("ldr") and "[pc" in insn.op_str:
            try:
                reg = insn.op_str.split(",")[0].strip()
                imm = int(insn.op_str.split("#")[1].split("]")[0].strip("+-"), 0)
                literal = (insn.address + 4) & ~3
                literal += imm
                loaded = struct.unpack("<I", elf.read(literal, 4))[0]
                target = elf.resolve(literal)
                # 直後に "add rX, pc" があれば PC 相対の補正を足す
                for nxt in insns[idx + 1:idx + 6]:
                    if nxt.mnemonic.startswith("add") and reg in nxt.op_str and "pc" in nxt.op_str:
                        target = elf.resolve(target)
                        break
                    if nxt.mnemonic not in ("mov", "movs", "mov.w", "ldr", "str", "str.w", "ldr.w", "add.w", "sub", "sub.w"):
                        break
                s = elf.cstring(target)
                if s and all(32 <= ord(c) < 127 or ord(c) > 160 for c in s):
                    extra = f'  ; -> "{s[:90]}"'
                else:
                    hexs = elf.read(target, 16).hex()
                    extra = f"  ; -> 0x{target:x} ({elf.section_of(target)}) {hexs}"
            except Exception:
                pass
        print(f"  {insn.address:08x}: {insn.mnemonic:8s} {insn.op_str}{extra}")


if __name__ == "__main__":
    elf = Elf()
    for name in sys.argv[1:]:
        disasm(elf, name)
        print()
