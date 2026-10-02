#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRHQ 超分 DNG 解码器 v1.0
=========================
专用解码 Sony ILCE-7C II (A7C2) 经 Transformer-JSR 超分输出的线性 DNG：
  - PhotometricInterpretation = 34892 (LinearRaw)
  - 图像数据 = 多条 SOF3 无损 JPEG 条带（16bit RGB）
  - TIFF 容器，IFD0 含 AsShotNeutral / ColorMatrix1

用法：
  双击（或 无参数运行）        → 图形界面
  命令行: python srhq_decoder.py <文件.dng> [输出.jpg]
  也可把 DNG 直接拖到 解码.bat 上

依赖（libs/ 目录，自包含）：numpy, Pillow, imagecodecs
"""
import os
import struct
import sys
import time
import threading

import numpy as np
import imagecodecs
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

# TIFF 基础类型字节数
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8}


class DNGError(Exception):
    pass


class TIFFReader:
    """最小化 TIFF/DNG 解析：只读我们需要的标签"""

    def __init__(self, path):
        self.d = open(path, "rb").read()
        if self.d[:2] == b"II":
            self.e = "<"
        elif self.d[:2] == b"MM":
            self.e = ">"
        else:
            raise DNGError("不是 TIFF/DNG 文件")
        magic = struct.unpack(self.e + "H", self.d[2:4])[0]
        if magic != 42:
            raise DNGError("TIFF magic 异常: %d" % magic)
        self.ifd0_off = struct.unpack(self.e + "I", self.d[4:8])[0]

    def u(self, fmt, off):
        return struct.unpack(self.e + fmt, self.d[off:off + struct.calcsize(self.e + fmt)])[0]

    def read_ifd(self, off):
        """返回 {tag: (type, count, data_offset)} 与 next IFD 偏移"""
        n = self.u("H", off)
        ent = {}
        for i in range(n):
            e = off + 2 + i * 12
            tag, typ, cnt = struct.unpack(self.e + "HHI", self.d[e:e + 8])
            size = TYPE_SIZE.get(typ, 4) * cnt
            if size <= 4:
                ent[tag] = (typ, cnt, e + 8)
            else:
                ent[tag] = (typ, cnt, self.u("I", e + 8))
        nxt = self.u("I", off + 2 + n * 12)
        return ent, nxt

    def values(self, ent, tag):
        """把标签数据解成 python 数值列表（RATIONAL/ SRATIONAL 返回 (a,b) 元组）"""
        if tag not in ent:
            return None
        typ, cnt, base = ent[tag]
        out = []
        for k in range(cnt):
            o = base + TYPE_SIZE.get(typ, 4) * k
            if typ in (1, 6, 7):
                out.append(self.d[o])
            elif typ in (3, 8):
                out.append(self.u("H", o))
            elif typ in (4, 9, 13):
                out.append(self.u("I", o))
            elif typ in (16, 17):
                out.append(self.u("Q", o))
            elif typ == 5:
                a, b = struct.unpack(self.e + "II", self.d[o:o + 8])
                out.append((a, b))
            elif typ == 10:
                a, b = struct.unpack(self.e + "ii", self.d[o:o + 8])
                out.append((a, b))
            elif typ == 2:
                end = self.d.find(b"\0", o, o + cnt)
                return self.d[o:o + (end if end > 0 else o + cnt)].decode("ascii", "replace")
            else:
                out.append(self.u("I", o))
        return out

    def ASCII_(self, ent, tag):
        if tag not in ent:
            return None
        typ, cnt, base = ent[tag]
        return self.d[base:base + cnt - 1].decode("ascii", "replace")

    @staticmethod
    def real(v):
        """RATIONAL 元组 → 浮点"""
        if isinstance(v, tuple):
            a, b = v
            return a / b if b else 0.0
        return float(v)


def find_raw_ifd(t):
    """找到 RAW 子 IFD：优先 NewSubFileType==0 且 Compression==7，否则取最大面积"""
    cands = []
    queue = [(t.ifd0_off, 0)]
    seen = set()
    while queue:
        off, depth = queue.pop(0)
        if off in seen or off == 0 or depth > 6:
            continue
        seen.add(off)
        ent, nxt = t.read_ifd(off)
        nsft = t.values(ent, 254)
        nsft = nsft[0] if nsft else -1
        comp = t.values(ent, 259)
        comp = comp[0] if comp else 0
        w = t.values(ent, 256)
        h = t.values(ent, 257)
        w = w[0] if w else 0
        h = h[0] if h else 0
        offs = t.values(ent, 273)
        cnts = t.values(ent, 279)
        if offs and cnts and w * h > 0:
            cands.append((nsft, comp, w, h, offs, cnts, off))
        # SubIFDs
        subs = t.values(ent, 330)
        if subs:
            for s in subs:
                queue.append((s, depth + 1))
        queue.append((nxt, depth + 1))

    if not cands:
        raise DNGError("找不到图像数据条带")
    # 首选 NewSubFileType==0 且 JPEG 压缩(7)
    for c in cands:
        if c[0] == 0 and c[1] == 7:
            return c
    # 退而求其次：面积最大的 JPEG 条带 IFD
    jpeg = [c for c in cands if c[1] == 7]
    pool = jpeg if jpeg else cands
    pool.sort(key=lambda c: c[2] * c[3], reverse=True)
    return pool[0]


def decode_dng(path, progress=None):
    """解码 → 返回 (RGB uint8 数组, 信息 dict)"""

    def pct(a, b, msg=""):
        if progress:
            progress(a, b, msg)

    pct(0, 100, "读取文件…")
    t = TIFFReader(path)
    ent0, _ = t.read_ifd(t.ifd0_off)
    make = t.ASCII_(ent0, 271) or "?"
    model = t.ASCII_(ent0, 272) or "?"
    info = {"make": make, "model": model}

    nsft, comp, W, H, offsets, counts, ifd_off = find_raw_ifd(t)
    if comp != 7:
        raise DNGError("该文件的图像数据不是 JPEG 压缩（Compression=%d），本工具不支持" % comp)
    ent_raw, _ = t.read_ifd(ifd_off)
    info["width"], info["height"] = W, H
    info["strips"] = len(offsets)

    # 黑位 / 白位 / 白平衡
    black = [0.0, 0.0, 0.0]
    bl = t.values(ent_raw, 50714)
    if bl:
        black = [t.real(v) for v in bl[:3]]
    white = 65535.0
    wl = t.values(ent_raw, 50717)
    if wl:
        white = float(wl[0])
    asn = t.values(ent0, 50728)
    if asn and len(asn) >= 3:
        neutral = [t.real(v) for v in asn[:3]]
    else:
        neutral = [1.0, 1.0, 1.0]
    wb = [1.0 / n if n > 1e-6 else 1.0 for n in neutral]
    m = max(wb) or 1.0
    wb = [x / m for x in wb]  # 最大通道归一为 1
    info["black"], info["white"], info["wb"] = black, white, wb

    # ---- 逐条带解码拼接（uint16）----
    full = np.empty((H, W, 3), dtype=np.uint16)
    row = 0
    fail = 0
    for i, (off, cnt) in enumerate(zip(offsets, counts)):
        pct(5 + 60 * i // len(offsets), 100, "解码条带 %d/%d" % (i + 1, len(offsets)))
        try:
            arr = imagecodecs.jpegsof3_decode(d_bytes_region(path, off, cnt))
        except Exception:
            fail += 1
            continue
        rows = arr.shape[0]
        if row + rows > H:
            rows = H - row
        if arr.shape[1] != W or arr.shape[2] != 3:
            raise DNGError("条带 %d 尺寸异常 (%s)，不是 3 通道 LinearRaw" % (i, arr.shape))
        full[row:row + rows] = arr[:rows]
        row += rows
        if row >= H:
            break
    if row < H:
        if row == 0:
            raise DNGError("所有条带解码失败，文件可能损坏")
        full[row:] = 0
    info["failed_strips"] = fail

    # ---- 渲染：黑位 → 白平衡 → 曝光 → sRGB Gamma ----
    pct(70, 100, "白平衡与色调渲染…")
    bl = np.array(black + [black[0]])[:3]
    wbv = np.array(wb, dtype=np.float64)
    # 曝光锚点：取抽样像素 99.5% 分位 → 0.92
    sample = full[::4, ::4].astype(np.float64)
    sample = (sample - bl) / (white - bl.max())
    sample *= wbv
    hi = np.percentile(sample, 99.5)
    if hi <= 1e-6:
        hi = 1.0
    info["exposure_scale"] = 0.92 / hi

    out8 = np.empty((H, W, 3), dtype=np.uint8)
    step = max(1, H // 16)
    for y0 in range(0, H, step):
        y1 = min(y0 + step, H)
        blk = full[y0:y1].astype(np.float64)
        blk = (blk - bl) / (white - bl.max())
        blk = np.clip(blk * wbv * (0.92 / hi), 0.0, 1.0)
        srgb = np.where(blk <= 0.0031308, blk * 12.92, 1.055 * blk ** (1 / 2.4) - 0.055)
        out8[y0:y1] = srgb * 255 + 0.5
        pct(70 + 25 * y0 // H, 100, "渲染 %d/%d 行" % (y0, H))
    pct(97, 100, "完成")
    return out8, info


_CACHE = {}


def d_bytes_region(path, off, cnt):
    """读文件区间（整文件已在内存就复用）"""
    key = path
    d = _CACHE.get(key)
    if d is None:
        d = open(path, "rb").read()
        if len(_CACHE) > 2:
            _CACHE.clear()
        _CACHE[key] = d
    return d[off:off + cnt]


def save_outputs(rgb8, src_path, out_dir=None, progress=None):
    base_dir = out_dir if out_dir else os.path.dirname(src_path)
    base = os.path.join(base_dir, os.path.splitext(os.path.basename(src_path))[0])
    out_full = base + "_decoded.jpg"
    if progress:
        progress(98, 100, "写全分辨率 JPG…")
    Image.fromarray(rgb8, "RGB").save(out_full, quality=92, subsampling=1)
    h, w = rgb8.shape[:2]
    out_q = base + "_decoded_quarter.jpg"
    Image.fromarray(rgb8, "RGB").resize((w // 4, h // 4), Image.LANCZOS).save(out_q, quality=90)
    return out_full, out_q


# ---------------- CLI ----------------
def run_cli(paths):
    for p in paths:
        p = p.strip('" ')
        if not os.path.isfile(p):
            print("找不到文件:", p)
            continue
        print("=" * 60)
        print("解码:", os.path.basename(p))
        t0 = time.time()
        try:
            rgb8, info = decode_dng(p, progress=lambda a, b, m: print("\r  %s" % m, end="", flush=True))
            print()
            full, quarter = save_outputs(rgb8, p, progress=lambda a, b, m: print("\r  %s" % m, end="", flush=True))
            print("\n  %s %s | %dx%d | 条带 %d (失败 %d) | WB %s | 耗时 %.1fs"
                  % (info["make"], info["model"], info["width"], info["height"],
                     info["strips"], info["failed_strips"],
                     ["%.2f" % x for x in info["wb"]], time.time() - t0))
            print("  输出:", full)
            print("       ", quarter)
        except DNGError as e:
            print("\n  [失败] %s" % e)
        except Exception as e:
            print("\n  [异常] %s: %s" % (type(e).__name__, e))


# ---------------- GUI ----------------
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk, messagebox
    import queue

    root = tk.Tk()
    root.title("SRHQ 超分 DNG 解码器 v1.1（Sony A7C2 专用）")
    root.geometry("640x400")
    root.configure(bg="#1e1e1e")

    fg = "#e0e0e0"
    status_var = tk.StringVar(value="把 DNG 拖到 解码.bat 上，或点击下方按钮选择文件")
    ui_queue = queue.Queue()   # 后台线程 → 主线程 的消息队列
    running = {"flag": False}

    tk.Label(root, text="SRHQ 超分 DNG 解码器", font=("Microsoft YaHei", 18, "bold"),
             bg="#1e1e1e", fg="#4fc3f7").pack(pady=(24, 4))
    tk.Label(root, text="适配 Sony ILCE-7C II / Transformer-JSR 超分输出（LinearRaw + 无损 JPEG 条带）",
             font=("Microsoft YaHei", 10), bg="#1e1e1e", fg=fg).pack()

    bar = ttk.Progressbar(root, length=540, mode="determinate")
    bar.pack(pady=18)
    tk.Label(root, textvariable=status_var, font=("Microsoft YaHei", 10),
             bg="#1e1e1e", fg="#aaaaaa").pack()
    log = tk.Text(root, height=8, bg="#141414", fg="#9cdcfe", font=("Consolas", 9),
                  relief="flat", state="disabled")
    log.pack(padx=20, pady=12, fill="both", expand=True)

    # 输出目录选择
    outframe = tk.Frame(root, bg="#1e1e1e")
    outframe.pack(fill="x", padx=20, before=log)
    outdir_var = tk.StringVar(value="（与源文件相同）")
    tk.Label(outframe, text="输出到:", font=("Microsoft YaHei", 10),
             bg="#1e1e1e", fg=fg).pack(side="left")
    out_entry = tk.Entry(outframe, textvariable=outdir_var, font=("Microsoft YaHei", 10),
                         bg="#141414", fg="#9cdcfe", relief="flat")
    out_entry.pack(side="left", fill="x", expand=True, padx=8)

    def pick_outdir():
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            outdir_var.set(os.path.normpath(d))

    tk.Button(outframe, text="浏览…", font=("Microsoft YaHei", 9), bg="#333333",
              fg=fg, relief="flat", padx=10, command=pick_outdir,
              cursor="hand2").pack(side="left")

    btn_decode = tk.Button(root, text="选择 DNG 文件并解码", font=("Microsoft YaHei", 12),
                           bg="#2d6cdf", fg="white", relief="flat", padx=24, pady=8,
                           cursor="hand2")
    btn_decode.pack(pady=6)

    def logln(s):
        log.configure(state="normal")
        log.insert("end", s + "\n")
        log.see("end")
        log.configure(state="disabled")

    def worker(path, out_dir_val):
        """后台线程：只做解码，UI 更新全部走 ui_queue（不碰任何 Tk 对象）"""
        def prog(a, b, m):
            ui_queue.put(("progress", a / b * 100, m))
        try:
            t0 = time.time()
            rgb8, info = decode_dng(path, progress=prog)
            if out_dir_val in ("", "（与源文件相同）") or not os.path.isdir(out_dir_val):
                out_dir_val = None
            full, quarter = save_outputs(rgb8, path, out_dir=out_dir_val)
            ui_queue.put(("log", "OK  %s %s  %dx%d  条带 %d(失败 %d)  耗时 %.1fs"
                          % (info["make"], info["model"], info["width"], info["height"],
                             info["strips"], info["failed_strips"], time.time() - t0)))
            ui_queue.put(("log", "    " + full))
            ui_queue.put(("log", "    " + quarter))
            ui_queue.put(("progress", 100, "完成 → " + os.path.basename(full)))
            ui_queue.put(("done", None))
        except Exception as e:
            print("[worker] FAIL: %s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)
            ui_queue.put(("log", "失败: %s: %s" % (type(e).__name__, e)))
            ui_queue.put(("progress", 0, "失败: %s" % e))
            ui_queue.put(("done", None))

    def poll():
        """主线程每 100ms 刷新一次 UI（tkinter 线程安全的标准做法）"""
        try:
            while True:
                item = ui_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    bar["value"] = item[1]
                    status_var.set(item[2])
                elif kind == "log":
                    logln(item[1])
                elif kind == "done":
                    running["flag"] = False
                    btn_decode.configure(state="normal", bg="#2d6cdf")
        except queue.Empty:
            pass
        root.after(100, poll)

    def start(paths):
        if running["flag"]:
            status_var.set("正在解码中，请等待完成…")
            return
        running["flag"] = True
        btn_decode.configure(state="disabled", bg="#555555")
        out_dir_val = outdir_var.get().strip()   # 主线程取值，传给后台线程
        threading.Thread(target=worker, args=(paths[0], out_dir_val), daemon=True).start()

    def pick():
        ps = filedialog.askopenfilenames(filetypes=[("DNG 图像", "*.dng"), ("所有文件", "*.*")])
        if ps:
            start(list(ps))

    btn_decode.configure(command=pick)

    def open_outdir():
        txt = log.get("1.0", "end").strip()
        for line in reversed(txt.split("\n")):
            if ":\\" in line and "decoded" in line:
                os.startfile(os.path.dirname(line.strip()))
                return
        status_var.set("还没有输出文件")

    tk.Button(root, text="打开输出文件夹", font=("Microsoft YaHei", 10),
              bg="#333333", fg=fg, relief="flat", padx=16, pady=4,
              command=open_outdir, cursor="hand2").pack(pady=4)

    poll()
    root.mainloop()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a.strip()]
    if args:
        run_cli(args)
    else:
        run_gui()
