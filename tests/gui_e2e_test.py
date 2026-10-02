# -*- coding: utf-8 -*-
"""GUI 端到端自动化测试：拦截文件对话框，模拟点击，驱动真实解码"""
import sys, time
sys.path.insert(0, r"D:/SRHQ_Decoder")
sys.path.insert(0, r"D:/SRHQ_Decoder/libs314")

import tkinter as tk
from tkinter import filedialog
import srhq_decoder

DNG = r"D:/jiang/SRHQ_Local_V15_3_5_K7_20261002_191031_036963/SRHQ_Local_V15_3_5_K7_20261002_191031_036963.dng"

# 拦截文件选择框
filedialog.askopenfilenames = lambda **kw: (DNG,)

def fake_mainloop(self, n=0):
    """替换 mainloop：手动泵事件循环，并自动触发'选择文件'按钮"""
    self.after(600, lambda: find_and_invoke(self, "选择 DNG"))
    t0 = time.time()
    import glob, os
    while True:
        try:
            self.update()
        except tk.TclError:
            print("[test] 窗口已关闭"); break
        if time.time() - t0 > 240:
            print("[test] 超时"); break
        outs = glob.glob(DNG.replace(".dng", "_decoded.jpg"))
        if outs and os.path.getmtime(outs[0]) > t0:
            print("[test] ✓ 检测到 GUI 解码输出:", outs[0])
            self.after(3000, self.destroy)   # 等 3 秒让后台写盘完成
            break
        time.sleep(0.05)

def find_and_invoke(w, text_part):
    for child in w.winfo_children():
        try:
            if text_part in child.cget("text"):
                child.invoke()
                print("[test] 已点击按钮:", child.cget("text"))
                return True
        except Exception:
            pass
        if find_and_invoke(child, text_part):
            return True
    return False

tk.Tk.mainloop = fake_mainloop
print("[test] 启动 GUI…")
srhq_decoder.run_gui()
print("[test] 结束")
