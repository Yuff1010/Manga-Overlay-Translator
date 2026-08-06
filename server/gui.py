"""Manga-Overlay-Translator 后端控制台（本地小工具，不入库）。

一个窗口搞定：启停后端、选 GPU/CPU 环境、可视化编辑 .env 里的 LLM 配置、看实时日志。
不改动后端任何代码——只是用子进程把 main.py 拉起来并捕获它的输出。

依赖：仅 Python 标准库（tkinter）。双击 start-gui.bat 运行。
"""
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))       # server/ 目录
ENV_FILE = os.path.join(HERE, ".env")
PING_PORT = 8765


# ---------- 纯逻辑：环境检测 ----------

def find_envs():
    """列出可用的 Python 解释器，GPU 优先。返回 [{name, python, gpu}]。"""
    home = os.path.expanduser("~")
    out = []
    # 项目自带 venv（是否 GPU 未知）
    for rel in (os.path.join(HERE, ".venv"), os.path.join(HERE, "..", ".venv")):
        py = os.path.join(rel, "Scripts", "python.exe")
        if os.path.isfile(py):
            out.append({"name": ".venv", "python": os.path.abspath(py), "gpu": None})
    # conda 环境：名字里带 vl / gpu 视为 GPU
    envs_dir = os.path.join(home, ".conda", "envs")
    for name in ("ocrt-vl", "ocrt-gpu", "ocrt"):
        py = os.path.join(envs_dir, name, "python.exe")
        if os.path.isfile(py):
            gpu = ("vl" in name) or ("gpu" in name)
            out.append({"name": name, "python": py, "gpu": gpu})
    # 默认选中最快的：明确 GPU 排最前，未知其次，CPU 最后
    out.sort(key=lambda e: {True: 0, None: 1, False: 2}[e["gpu"]])
    return out


def env_label(e):
    tag = "GPU" if e["gpu"] else ("CPU" if e["gpu"] is False else "?")
    return f"{e['name']} ({tag})"


def env_choices(envs):
    """把技术环境名翻成用户看得懂的描述，只讲快慢和推荐。

    envs 已按 GPU 优先排序，所以第一个 GPU 就是默认推荐项。
    """
    labels, gpu_n, cpu_n = [], 0, 0
    for e in envs:
        if e["gpu"] is True:
            gpu_n += 1
            labels.append("显卡加速（推荐 · 最快）" if gpu_n == 1 else f"显卡加速（备用 {gpu_n}）")
        elif e["gpu"] is False:
            cpu_n += 1
            labels.append("CPU 运行（较慢）" if cpu_n == 1 else f"CPU 运行（{cpu_n}）")
        else:
            labels.append("自定义环境（.venv）")
    return labels


# ---------- 纯逻辑：.env 读写 ----------

def read_profiles(path=ENV_FILE):
    """解析 .env 里的 LLM 配置，返回 (profiles, other_lines)。

    profiles: [{name, base_url, api_key, models}]，与后端 _load_profiles 同格式。
    other_lines: 非 LLM_ 的原始行，保存时原样写回，避免丢注释或其它变量。
    """
    profiles, other = {}, []
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            raw = line.rstrip("\n")
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                other.append(raw)
                continue
            key, val = s.split("=", 1)
            key, val = key.strip(), val.strip()
            if not key.startswith("LLM_"):
                other.append(raw)
                continue
            rest = key[4:]                       # 去掉 LLM_
            if rest[:1].isdigit():
                idx, field = rest.split("_", 1)  # LLM_2_NAME -> ("2","NAME")
            else:
                idx, field = "1", rest           # LLM_NAME -> ("1","NAME")
            profiles.setdefault(idx, {"name": "", "base_url": "", "api_key": "", "models": ""})
            m = {"NAME": "name", "BASE_URL": "base_url", "API_KEY": "api_key",
                 "MODEL": "models", "MODELS": "models"}.get(field)
            if m == "models":
                cur = profiles[idx]["models"]        # MODEL 和 MODELS 都并进 models，逗号拼
                profiles[idx]["models"] = (cur + "," + val) if (cur and val) else (val or cur)
            elif m:
                profiles[idx][m] = val
    ordered = [profiles[k] for k in sorted(profiles, key=lambda x: int(x))]
    if not ordered:
        ordered = [{"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                    "api_key": "", "models": "deepseek-chat"}]
    return ordered, other


def write_profiles(profiles, other_lines, path=ENV_FILE):
    """把供应商配置写回 .env，保留 other_lines 里的非 LLM_ 内容。"""
    keep = [l for l in other_lines if not l.strip().startswith("LLM_")]
    lines = list(keep)
    if lines and lines[-1].strip():
        lines.append("")
    for i, p in enumerate(profiles, 1):
        pre = "LLM_" if i == 1 else f"LLM_{i}_"
        lines.append(f"{pre}NAME={p['name']}")
        lines.append(f"{pre}BASE_URL={p['base_url']}")
        lines.append(f"{pre}API_KEY={p['api_key']}")
        lines.append(f"{pre}MODELS={p['models']}")
        lines.append("")
    open(path, "w", encoding="utf-8").write("\n".join(lines).rstrip() + "\n")


# ---------- GUI ----------

class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.log_q = queue.Queue()
        self.envs = find_envs()
        self.prof_rows = []          # 每组供应商的 Entry 控件
        self.other_lines = []

        root.title("Manga Translator 控制台")
        root.geometry("560x680")
        root.configure(bg="#f4f4f4")
        FONT = ("Microsoft YaHei", 10)
        root.option_add("*Font", FONT)

        # ── 顶部：状态 + 环境 + 启停 ──
        top = tk.Frame(root, bg="#f4f4f4"); top.pack(fill="x", padx=12, pady=(12, 6))
        self.dot = tk.Canvas(top, width=14, height=14, bg="#f4f4f4", highlightthickness=0)
        self.dot.pack(side="left")
        self._dot = self.dot.create_oval(2, 2, 12, 12, fill="#c0392b", outline="")
        self.status = tk.Label(top, text="已停止", bg="#f4f4f4"); self.status.pack(side="left", padx=(6, 14))

        tk.Label(top, text="运行方式", bg="#f4f4f4").pack(side="left")
        self.env_var = tk.StringVar()
        self.choices = env_choices(self.envs)
        self.env_box = ttk.Combobox(top, values=self.choices or ["（未找到环境）"],
                                    textvariable=self.env_var, state="readonly", width=18)
        self.env_box.current(0); self.env_box.pack(side="left", padx=6)

        self.btn_start = tk.Button(top, text="▶ 启动", bg="#2d7d46", fg="white",
                                   width=8, command=self.start)
        self.btn_stop = tk.Button(top, text="■ 停止", bg="#7a3030", fg="white",
                                  width=8, state="disabled", command=self.stop)
        self.btn_stop.pack(side="right"); self.btn_start.pack(side="right", padx=6)

        tk.Label(root, text=f"http://127.0.0.1:{PING_PORT}", bg="#f4f4f4",
                 fg="#666").pack(anchor="w", padx=34)

        # ── 中部：LLM 配置 ──
        cfg = tk.LabelFrame(root, text=" LLM 供应商 ", bg="#f4f4f4", padx=8, pady=6)
        cfg.pack(fill="x", padx=12, pady=8)
        self.cfg_body = tk.Frame(cfg, bg="#f4f4f4"); self.cfg_body.pack(fill="x")
        bar = tk.Frame(cfg, bg="#f4f4f4"); bar.pack(fill="x", pady=(6, 0))
        tk.Button(bar, text="+ 添加供应商", command=self.add_blank).pack(side="left")
        tk.Button(bar, text="保存配置", bg="#3a6ea5", fg="white",
                  command=self.save_cfg).pack(side="right")
        self.load_cfg()

        # ── 底部：日志 ──
        lf = tk.LabelFrame(root, text=" 日志 ", bg="#f4f4f4"); lf.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log = scrolledtext.ScrolledText(lf, height=12, bg="#1e1e1e", fg="#ddd",
                                             font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self.drain_log)

    # ---- LLM 配置区 ----
    def load_cfg(self):
        profs, self.other_lines = read_profiles()
        for p in profs:
            self.add_row(p)

    def add_blank(self):
        self.add_row({"name": "", "base_url": "", "api_key": "", "models": ""})

    def add_row(self, p):
        idx = len(self.prof_rows) + 1
        fr = tk.LabelFrame(self.cfg_body, text=f" 供应商 {idx} ", bg="#f4f4f4",
                           padx=6, pady=4)
        fr.pack(fill="x", pady=3)
        ents = {}
        for label, key, hide in (("名称", "name", False), ("接口", "base_url", False),
                                  ("密钥", "api_key", True), ("模型", "models", False)):
            row = tk.Frame(fr, bg="#f4f4f4"); row.pack(fill="x", pady=1)
            tk.Label(row, text=label, width=4, bg="#f4f4f4").pack(side="left")
            e = tk.Entry(row, show="●" if hide else "")
            e.insert(0, p.get(key, "")); e.pack(side="left", fill="x", expand=True)
            if hide:
                tk.Button(row, text="👁", width=2,
                          command=lambda en=e: en.config(show="" if en["show"] else "●")
                          ).pack(side="left", padx=(4, 0))
            ents[key] = e
        self.prof_rows.append((fr, ents))

    def save_cfg(self):
        profs = [{k: e.get().strip() for k, e in ents.items()} for _, ents in self.prof_rows]
        profs = [p for p in profs if p["base_url"]]      # 空组丢弃
        if not profs:
            messagebox.showwarning("配置", "至少要有一个填了接口地址的供应商。")
            return
        try:
            write_profiles(profs, self.other_lines)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e)); return
        msg = "已保存到 .env。"
        if self.proc:
            msg += "\n配置在后端启动时读取，请点“停止”再“启动”让它生效。"
        messagebox.showinfo("配置", msg)

    # ---- 进程管理 ----
    def start(self):
        if self.proc:
            return
        if not self.envs:
            messagebox.showerror("启动", "没找到可用的 Python 环境。"); return
        i = self.env_box.current()
        env = self.envs[i]
        # 日志里同时给友好描述和真实环境名，方便排查
        self.append(f"[gui] 用「{self.choices[i]}」启动后端（{env['name']}）…\n")
        try:
            self.proc = subprocess.Popen(
                [env["python"], "main.py"], cwd=HERE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("启动失败", str(e)); self.proc = None; return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.set_running(True)

    def _reader(self, proc):
        for line in proc.stdout:
            self.log_q.put(line)
        self.log_q.put(f"[gui] 后端进程已退出（码 {proc.wait()}）\n")
        self.log_q.put(("__stopped__",))

    def stop(self):
        if not self.proc:
            return
        self.append("[gui] 正在停止后端…\n")
        try:
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        self.proc = None
        self.set_running(False)

    def set_running(self, on):
        self.dot.itemconfig(self._dot, fill="#2ecc71" if on else "#c0392b")
        self.status.config(text="运行中" if on else "已停止")
        self.btn_start.config(state="disabled" if on else "normal")
        self.btn_stop.config(state="normal" if on else "disabled")
        self.env_box.config(state="disabled" if on else "readonly")

    # ---- 日志刷新 ----
    def drain_log(self):
        try:
            while True:
                item = self.log_q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__stopped__":
                    self.proc = None; self.set_running(False)
                else:
                    self.append(item)
        except queue.Empty:
            pass
        self.root.after(120, self.drain_log)

    def append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        # 限制行数，别让日志无限涨
        if int(self.log.index("end-1c").split(".")[0]) > 800:
            self.log.delete("1.0", "200.0")
        self.log.config(state="disabled")

    def on_close(self):
        if self.proc and not messagebox.askokcancel("退出", "后端还在运行，退出会一并停止。继续？"):
            return
        self.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
