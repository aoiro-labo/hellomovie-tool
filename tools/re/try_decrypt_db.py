"""マスターキー候補から AES 鍵を導出し、packed.db の復号を試す。"""
import base64, hashlib, sys
from pathlib import Path

from Crypto.Cipher import AES

sys.stdout.reconfigure(encoding="utf-8")

SO = Path(r"C:\Users\aoiro\Documents\hellomovie-tool\analysis\native\armeabi-v7a\libsapkit.so")
DB = Path(r"C:\Users\aoiro\Documents\hellomovie-tool\hellomovie_downloads\decrypted\2144_voiceguide_v1\voiceguide\db\packed.db").read_bytes()
MASTER = b"iJBP8RHZgYJHZC7DW7bTXn3Bb7t4ARgMkFsWK48xL9gwcPmQNryADSNZFcjpCA2j2AHbRYrdKFsUYNZ5F8AZ5amVnx6UdRPnJmA6urHp2g7YgEdL3pFTsC7D7xpf2Dc8"
JAVA_K1 = "dnRaeeIGIRt6hG7ADmrT29rEKX9kvpdQYgsGj0Xyi2arycBm3OfvxY+GJqd5dL/d7Y3zlHPCbh+dyNnb77BMFECcyaAMjQqXHS03nWy46ZXCmpk9ZPU3XeDTFFKT/+wf"
JAVA_K2 = "XXBU6d12wboOvdLX4tVjY8NJBBcs7/Bzx9wC6djwhkN5A6unptD9om8okI5I0Ic7xBmNLj7uRonJDozw4VKcZw="


def b64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def score(data: bytes) -> float:
    head = data[:4096]
    if not head:
        return 0.0
    zeros = head.count(0) / len(head)
    printable = sum(1 for b in head if 32 <= b < 127) / len(head)
    small = 0
    total = 0
    for i in range(0, len(head) - 3, 4):
        value = int.from_bytes(head[i:i + 4], 'little')
        total += 1
        if value < 100000:
            small += 1
    return zeros * 0.35 + printable * 0.25 + (small / max(total, 1)) * 0.4


def derivations(name: str, blob: bytes) -> dict:
    out = {}
    for length in (16, 32):
        out[f"{name}[:{length}]"] = blob[:length]
        out[f"{name}[-{length}:]"] = blob[-length:]
    out[f"md5({name})"] = hashlib.md5(blob).digest()
    out[f"sha1({name})[:16]"] = hashlib.sha1(blob).digest()[:16]
    out[f"sha256({name})[:16]"] = hashlib.sha256(blob).digest()[:16]
    out[f"sha256({name})[16:]"] = hashlib.sha256(blob).digest()[16:]
    if len(blob) >= 32:
        out[f"{name} xor halves"] = bytes(a ^ b for a, b in zip(blob[:16], blob[16:32]))
    return out


print(f"packed.db: {len(DB)} バイト / 先頭 {DB[:16].hex()}")
print(f"復号前スコア: {score(DB):.4f}\n")

cands = {}
cands.update(derivations("master", MASTER))
cands.update(derivations("java1", b64(JAVA_K1)))
cands.update(derivations("java2", b64(JAVA_K2)))

results = []
for name, key in cands.items():
    for mode in ("ECB", "CBC0"):
        try:
            if mode == "ECB":
                out = AES.new(key, AES.MODE_ECB).decrypt(DB[:16 * 64])
            else:
                out = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16).decrypt(DB[:16 * 64])
        except Exception:
            continue
        results.append((score(out), name, mode, out[:32].hex()))

results.sort(reverse=True)
for s, name, mode, head in results[:8]:
    print(f"  スコア {s:.4f}  {name:24s} {mode:5s} {head}")

# 二段構え: java1/java2 をマスターキーで復号してから DB 鍵に使う
print("\n--- 二段構え ---")
results2 = []
for keyname, blob in (("java1", b64(JAVA_K1)), ("java2", b64(JAVA_K2))):
    for dname, mkey in derivations("master", MASTER).items():
        if len(mkey) not in (16, 32):
            continue
        try:
            mid = AES.new(mkey, AES.MODE_ECB).decrypt(blob)
        except Exception:
            continue
        for sub, subkey in derivations(keyname, mid).items():
            if len(subkey) not in (16, 32):
                continue
            try:
                out = AES.new(subkey, AES.MODE_ECB).decrypt(DB[:16 * 64])
            except Exception:
                continue
            results2.append((score(out), f"{keyname}->{dname}->{sub}", "ECB", out[:32].hex()))
results2.sort(reverse=True)
for s, name, mode, head in results2[:8]:
    print(f"  スコア {s:.4f}  {name:44s} {head}")
