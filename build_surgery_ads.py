#!/usr/bin/env python3
"""Build a stock-shaped .ADS whose inner ZIP differs only in one member.

The stock inner ZIP is kept byte-for-byte except for the target member
(default: update.sh), which is replaced with the supplied payload.

Outputs a .ADS plus a verification report. Requires a stock .ADS as reference
for the 112-byte header template and product id.

usage:
  build_surgery_ads.py --stock STOCK.ADS --member update.sh --payload FILE \
      --out OUT.ADS [--store]
"""
import argparse
import io
import os
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ads_decode_menu as A  # noqa: E402

EOCD_SIG = b"PK\x05\x06"
CD_SIG = b"PK\x01\x02"
LOC_SIG = b"PK\x03\x04"


def decode_header(ads: bytes):
    """Decrypt the 112-byte file header."""
    h = A.des3_ecb_decrypt(ads[:112], A.K1, A.K2)
    return {
        "crc": struct.unpack("<I", h[0:4])[0],
        "size": struct.unpack("<I", h[4:8])[0],
        "product_id": struct.unpack("<I", h[0xC:0x10])[0],
        "vendor": h[0x26:0x2D].decode("latin1"),
        "usb_host": h[0x3A:0x41].decode("latin1"),
    }


def _find_eocd(buf: bytes) -> int:
    i = buf.rfind(EOCD_SIG)
    if i < 0:
        raise ValueError("no EOCD")
    return i


def _parse_central(buf: bytes):
    eocd = _find_eocd(buf)
    nent = struct.unpack_from("<H", buf, eocd + 10)[0]
    cd_size, cd_off = struct.unpack_from("<II", buf, eocd + 12)
    entries = []
    p = cd_off
    for _ in range(nent):
        if buf[p:p + 4] != CD_SIG:
            raise ValueError("bad central dir at %#x" % p)
        (ver_made, ver_need, flags, method, mtime, mdate,
         crc, csize, usize, nlen, elen, clen, disk, iattr, eattr,
         loff) = struct.unpack_from("<HHHHHHIIIHHHHHII", buf, p + 4)
        name = buf[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        entries.append(dict(
            cd_pos=p, ver_made=ver_made, ver_need=ver_need, flags=flags,
            method=method, mtime=mtime, mdate=mdate, crc=crc, csize=csize,
            usize=usize, nlen=nlen, elen=elen, clen=clen, iattr=iattr,
            eattr=eattr, loff=loff, name=name,
            cd_bytes=buf[p:p + 46 + nlen + elen + clen],
        ))
        p += 46 + nlen + elen + clen
    eocd_bytes = buf[eocd:]
    return entries, eocd, eocd_bytes


def _parse_local(buf: bytes, loff: int):
    if buf[loff:loff + 4] != LOC_SIG:
        raise ValueError("bad local header at %#x" % loff)
    (ver, flags, method, mtime, mdate, crc, csize, usize,
     nlen, elen) = struct.unpack_from("<HHHHHIIIHH", buf, loff + 4)
    data_off = loff + 30 + nlen + elen
    return dict(loff=loff, ver=ver, flags=flags, method=method,
                mtime=mtime, mdate=mdate, crc=crc, csize=csize,
                usize=usize, nlen=nlen, elen=elen, data_off=data_off,
                hdr=buf[loff:data_off])


def zip_surgery(zip_bytes: bytes, target: str, new_data: bytes,
                force_store: bool = False) -> bytes:
    """Return a ZIP identical to input except the `target` member's data."""
    entries, eocd, eocd_bytes = _parse_central(zip_bytes)

    if force_store:
        method, comp = 0, new_data
    else:
        method = 8
        c = zlib.compressobj(6, zlib.DEFLATED, -15)
        comp = c.compress(new_data) + c.flush()
    new_crc = zlib.crc32(new_data) & 0xFFFFFFFF

    found = False
    out = bytearray()
    new_loffs = {}
    for e in entries:
        lo = _parse_local(zip_bytes, e["loff"])
        if lo["flags"] & 0x08:
            raise ValueError("data descriptor not supported (%s)" % e["name"])
        old_data = zip_bytes[lo["data_off"]:lo["data_off"] + lo["csize"]]

        new_loff = len(out)
        new_loffs[e["name"]] = new_loff

        if e["name"] == target:
            found = True
            # patch local header fields in place
            lh = bytearray(lo["hdr"])
            struct.pack_into("<H", lh, 8, method)
            struct.pack_into("<I", lh, 14, new_crc)
            struct.pack_into("<I", lh, 18, len(comp))
            struct.pack_into("<I", lh, 22, len(new_data))
            out += lh + comp
            e["method"] = method
            e["crc"] = new_crc
            e["csize"] = len(comp)
            e["usize"] = len(new_data)
        else:
            out += lo["hdr"] + old_data

    if not found:
        raise SystemExit("member %r not found in stock ZIP" % target)

    # rebuild central directory: copy each original CD entry, patch fields
    cd = bytearray()
    for e in entries:
        cb = bytearray(e["cd_bytes"])
        struct.pack_into("<H", cb, 10, e["method"])
        struct.pack_into("<I", cb, 16, e["crc"])
        struct.pack_into("<I", cb, 20, e["csize"])
        struct.pack_into("<I", cb, 24, e["usize"])
        struct.pack_into("<I", cb, 42, new_loffs[e["name"]])
        cd += cb

    cd_off = len(out)
    out += cd

    # EOCD: patch cd offset/size, keep everything else (incl. comment length)
    eo = bytearray(eocd_bytes)
    struct.pack_into("<I", eo, 12, len(cd))
    struct.pack_into("<I", eo, 16, cd_off)
    out += eo
    return bytes(out)


def pad_zip_comment(zip_bytes: bytes, target_mult: int = 8, header_len: int = 0x34):
    """Append ZIP comment bytes so header length plus zip size is aligned to target_mult."""
    need = (-(header_len + len(zip_bytes))) % target_mult
    if need == 0:
        return zip_bytes, 0
    eocd = _find_eocd(zip_bytes)
    old_clen = struct.unpack_from("<H", zip_bytes, eocd + 20)[0]
    buf = bytearray(zip_bytes)
    struct.pack_into("<H", buf, eocd + 20, old_clen + need)
    buf += b"\x00" * need
    return bytes(buf), need


def build(stock_path: str, member: str, payload_path: str, out_path: str,
          store: bool = False, pad: bool = False, emit_zip: str = None,
          verbose: bool = True):
    """Build a .ADS by replacing one member in the stock inner ZIP."""
    stock = open(stock_path, "rb").read()
    stream = A.decode(stock)
    if stream[:4] != struct.pack("<I", struct.unpack_from("<I", stream, 0)[0]):
        pass
    z0 = stream.find(LOC_SIG)
    if z0 != 0x34:
        raise SystemExit("unexpected ZIP offset %#x (want 0x34)" % z0)
    stock_zip = stream[z0:]

    payload = open(payload_path, "rb").read()
    new_zip = zip_surgery(stock_zip, member, payload, force_store=store)
    padn = 0
    if pad:
        new_zip, padn = pad_zip_comment(new_zip)
    if emit_zip:
        open(emit_zip, "wb").write(new_zip)

    body = new_zip
    stream2 = struct.pack("<III", A.check_word(body), len(body), 7) + b"\x00" * (0x34 - 12) + body
    enc = A.confuse(stream2)
    hdr = A.rebuild_header(stock[:112], stream2, enc)
    ads = hdr + enc
    open(out_path, "wb").write(ads)

    if verbose:
        print("stock        :", stock_path, len(stock), "B")
        print("member       :", member, "->", len(payload), "B", "(stored)" if store else "(deflate)")
        print("zip comment  :", padn, "pad byte(s)")
        print("stream       :", len(stream2), "B  zip:", len(new_zip), "B")
        print("output       :", out_path, len(ads), "B")
    return ads


def verify(ads_path: str, stock_path: str, member: str, payload_path: str):
    """Verify the rebuilt .ADS: header fields, member set, CRCs, and payload."""
    ads = open(ads_path, "rb").read()
    hdr = decode_header(ads)
    st = A.decode(ads)
    z0 = st.find(LOC_SIG)
    z = st[z0:]
    ok = True
    def chk(cond, msg):
        nonlocal ok
        ok = ok and cond
        print(("  PASS  " if cond else "  FAIL  ") + msg)

    print("verify", ads_path)
    chk(hdr["product_id"] == 17001, "product_id=17001 (got %d)" % hdr["product_id"])
    chk(hdr["size"] == len(st), "header size field matches decoded length")
    fchk = A.check_word(ads[112:], sum(A.des3_ecb_decrypt(ads[:112], A.K1, A.K2)[4:]))
    chk(fchk == hdr["crc"], "file check word")
    rchk, rlen = struct.unpack_from("<II", st, 0)
    chk(A.check_word(st[0x34:0x34 + rlen]) == rchk, "container record check word")
    chk(rlen == len(st) - 0x34, "container record length")

    stock_zip = A.decode(open(stock_path, "rb").read())[0x34:]
    se, _, _ = _parse_central(stock_zip)
    ne, _, _ = _parse_central(z)
    smap = {e["name"]: e for e in se}
    nmap = {e["name"]: e for e in ne}
    chk(set(smap) == set(nmap), "member name set identical (%d)" % len(nmap))

    byte_ident = 0
    diffs = []
    for name, e in smap.items():
        n = nmap[name]
        s_local = _parse_local(stock_zip, e["loff"])
        n_local = _parse_local(z, n["loff"])
        s_data = stock_zip[s_local["data_off"]:s_local["data_off"] + e["csize"]]
        n_data = z[n_local["data_off"]:n_local["data_off"] + n["csize"]]
        if s_data == n_data and s_local["hdr"] == n_local["hdr"]:
            byte_ident += 1
        else:
            diffs.append(name)
    chk(diffs == [member], "only %r differs" % member)
    print("  INFO  %d/%d members byte-identical" % (byte_ident, len(smap)))

    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        bad = zf.testzip()
        chk(bad is None, "all ZIP CRCs valid (bad=%r)" % bad)
        got = zf.read(member)
        chk(got == open(payload_path, "rb").read(), "payload member round-trips byte-exact")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--member", default="update.sh")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--pad", action="store_true",
                    help="pad ZIP comment for 8-alignment (tiny packages only)")
    ap.add_argument("--emit-zip", help="also write the new inner ZIP here (for QEMU harness)")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    build(a.stock, a.member, a.payload, a.out, store=a.store, pad=a.pad,
          emit_zip=a.emit_zip)
    if a.verify:
        sys.exit(0 if verify(a.out, a.stock, a.member, a.payload) else 1)
