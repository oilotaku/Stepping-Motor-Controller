"""
高斯向量圖模擬 — 獨立 tkinter 小工具

包一層介面在 gaussian_vector_sim.py 外面：勾選要模擬的軸、輸入各軸中心／
高斯寬度與其餘條件，按下「執行模擬」後在視窗內直接顯示梯度向量圖與爬升
搜尋統計。完全獨立於 main_ai.py，不連接硬體、不 import 任何 DS102 相關模組。
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from gaussian_vector_sim import AXES, GaussianFieldConfig, build_vector_field_figure, simulate_hill_climb, trace_hill_climb

DEFAULT_OUTDIR = Path("gaussian_sim_output")


class GaussianVectorSimApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("高斯向量圖模擬")
        self.root.geometry("1200x800")

        self._axis_rows: dict[str, dict] = {}
        self._fig_canvas: FigureCanvasTkAgg | None = None
        self._running = False

        self._build_layout()

    # ---------- 版面 ----------

    def _build_layout(self) -> None:
        paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        form_frame = ttk.Frame(paned, padding=10)
        result_frame = ttk.Frame(paned, padding=10)
        paned.add(form_frame, weight=0)
        paned.add(result_frame, weight=1)

        self._build_form(form_frame)
        self._build_result_area(result_frame)

    def _build_form(self, parent: ttk.Frame) -> None:
        axes_box = ttk.Labelframe(parent, text="模擬軸（勾選要模擬的軸，可複選）")
        axes_box.pack(fill=tk.X, pady=(0, 8))

        header = ttk.Frame(axes_box)
        header.pack(fill=tk.X, padx=4, pady=(4, 0))
        for col, text in enumerate(("軸", "中心 (pulse)", "σ 寬度 (pulse)")):
            ttk.Label(header, text=text, width=12 if col else 6).grid(row=0, column=col, padx=2)

        for axis in AXES:
            row = ttk.Frame(axes_box)
            row.pack(fill=tk.X, padx=4, pady=1)
            var_used = tk.BooleanVar(value=(axis in ("X", "Y")))
            var_center = tk.StringVar(value="0")
            var_sigma = tk.StringVar(value="5000")
            ttk.Checkbutton(row, text=axis, variable=var_used, width=4).grid(row=0, column=0, padx=2)
            ttk.Entry(row, textvariable=var_center, width=12).grid(row=0, column=1, padx=2)
            ttk.Entry(row, textvariable=var_sigma, width=12).grid(row=0, column=2, padx=2)
            self._axis_rows[axis] = {"used": var_used, "center": var_center, "sigma": var_sigma}

        cond_box = ttk.Labelframe(parent, text="場條件")
        cond_box.pack(fill=tk.X, pady=8)
        self._peak_power = self._add_field(cond_box, "尖峰功率 (dBm)", "-10")
        self._noise_floor = self._add_field(cond_box, "雜訊底 (dBm)", "-60")
        self._noise_std = self._add_field(cond_box, "量測雜訊標準差 (dB)", "0.05")
        self._grid_min = self._add_field(cond_box, "繪圖範圍下限 (pulse)", "-15000")
        self._grid_max = self._add_field(cond_box, "繪圖範圍上限 (pulse)", "15000")
        self._grid_points = self._add_field(cond_box, "格點數（每軸）", "25")

        sim_box = ttk.Labelframe(parent, text="爬升搜尋統計條件")
        sim_box.pack(fill=tk.X, pady=8)
        self._trials = self._add_field(sim_box, "模擬次數", "200")
        self._max_iter = self._add_field(sim_box, "每次最大步數", "200")
        self._tol_pulse = self._add_field(sim_box, "收斂誤差門檻 (pulse)", "50")
        self._step_gain = self._add_field(sim_box, "初始步長比例", "0.05")
        self._seed = self._add_field(sim_box, "亂數種子", "42")

        outdir_box = ttk.Labelframe(parent, text="輸出資料夾")
        outdir_box.pack(fill=tk.X, pady=8)
        self._outdir_var = tk.StringVar(value=str(DEFAULT_OUTDIR))
        row = ttk.Frame(outdir_box)
        row.pack(fill=tk.X, padx=4, pady=4)
        ttk.Entry(row, textvariable=self._outdir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="瀏覽…", command=self._browse_outdir).pack(side=tk.LEFT, padx=(4, 0))

        self._run_btn = ttk.Button(parent, text="執行模擬", command=self._on_run_clicked)
        self._run_btn.pack(fill=tk.X, pady=(8, 0))
        self._status_var = tk.StringVar(value="就緒")
        ttk.Label(parent, textvariable=self._status_var, foreground="#555").pack(fill=tk.X, pady=(4, 0))

    def _add_field(self, parent: ttk.Frame, label: str, default: str) -> tk.StringVar:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(row, text=label, width=20).pack(side=tk.LEFT)
        var = tk.StringVar(value=default)
        ttk.Entry(row, textvariable=var, width=14).pack(side=tk.LEFT)
        return var

    def _build_result_area(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        self._figure_holder = ttk.Frame(notebook)
        notebook.add(self._figure_holder, text="向量圖（含代表路徑）")
        ttk.Label(self._figure_holder, text="按左側「執行模擬」後，向量圖會顯示在這裡",
                  foreground="#888").pack(expand=True)

        steps_tab = ttk.Frame(notebook)
        notebook.add(steps_tab, text="逐步記錄")
        columns = ("iter", "pos", "power_dbm", "grad_mag", "step_size")
        self._steps_tree = ttk.Treeview(steps_tab, columns=columns, show="headings")
        headings = {"iter": "步", "pos": "位置 (pulse)", "power_dbm": "量測功率 (dBm)",
                    "grad_mag": "梯度大小", "step_size": "步長 (pulse)"}
        widths = {"iter": 50, "pos": 340, "power_dbm": 120, "grad_mag": 100, "step_size": 100}
        for col in columns:
            self._steps_tree.heading(col, text=headings[col])
            self._steps_tree.column(col, width=widths[col], anchor=tk.CENTER if col != "pos" else tk.W)
        steps_scroll = ttk.Scrollbar(steps_tab, orient=tk.VERTICAL, command=self._steps_tree.yview)
        self._steps_tree.configure(yscrollcommand=steps_scroll.set)
        self._steps_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        steps_scroll.pack(side=tk.LEFT, fill=tk.Y)

        self._stats_text = tk.Text(parent, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self._stats_text.pack(fill=tk.X, pady=(8, 0))

    def _browse_outdir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self._outdir_var.get() or ".")
        if chosen:
            self._outdir_var.set(chosen)

    # ---------- 執行 ----------

    def _collect_config(self) -> tuple[GaussianFieldConfig, tuple[float, float], dict]:
        selected = [axis for axis in AXES if self._axis_rows[axis]["used"].get()]
        if not selected:
            raise ValueError("至少要勾選一個軸")

        center = np.array([float(self._axis_rows[a]["center"].get()) for a in selected])
        sigma = np.array([float(self._axis_rows[a]["sigma"].get()) for a in selected])
        if np.any(sigma <= 0):
            raise ValueError("σ 寬度必須大於 0")

        cfg = GaussianFieldConfig(
            axis_names=selected, center=center, sigma=sigma,
            peak_power_dbm=float(self._peak_power.get()),
            noise_floor_dbm=float(self._noise_floor.get()),
            noise_std_db=float(self._noise_std.get()),
            seed=int(self._seed.get()),
        )
        grid_range = (float(self._grid_min.get()), float(self._grid_max.get()))
        if grid_range[1] <= grid_range[0]:
            raise ValueError("繪圖範圍上限必須大於下限")

        run_params = {
            "grid_points": int(self._grid_points.get()),
            "n_trials": int(self._trials.get()),
            "max_iter": int(self._max_iter.get()),
            "tol_pulse": float(self._tol_pulse.get()),
            "step_gain": float(self._step_gain.get()),
        }
        return cfg, grid_range, run_params

    def _on_run_clicked(self) -> None:
        if self._running:
            return
        try:
            cfg, grid_range, run_params = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("參數錯誤", str(exc))
            return

        self._running = True
        self._run_btn.state(["disabled"])
        self._status_var.set("模擬中…")
        threading.Thread(target=self._run_worker, args=(cfg, grid_range, run_params), daemon=True).start()

    def _run_worker(self, cfg: GaussianFieldConfig, grid_range: tuple[float, float], run_params: dict) -> None:
        try:
            trace_rng = np.random.default_rng(cfg.seed)
            trace = trace_hill_climb(
                cfg, grid_range, run_params["max_iter"], run_params["tol_pulse"],
                run_params["step_gain"], trace_rng,
            )
            path = np.array([s["pos"] for s in trace["steps"]])

            fig = build_vector_field_figure(cfg, grid_range, run_params["grid_points"],
                                             fixed_values=cfg.center, path=path)

            batch_rng = np.random.default_rng(cfg.seed + 1)
            stats = simulate_hill_climb(
                cfg, grid_range, run_params["n_trials"], run_params["max_iter"],
                run_params["tol_pulse"], run_params["step_gain"], batch_rng,
            )
            outdir = Path(self._outdir_var.get() or DEFAULT_OUTDIR)
            outdir.mkdir(parents=True, exist_ok=True)
            png_path = outdir / "gaussian_vector_field.png"
            fig.savefig(png_path, dpi=150)
        except Exception as exc:  # noqa: BLE001 — 背景執行緒例外要回主執行緒才能跳訊息框
            self.root.after(0, lambda exc=exc: self._on_run_failed(exc))
            return
        self.root.after(0, lambda: self._on_run_done(fig, stats, trace, cfg, png_path))

    def _on_run_failed(self, exc: Exception) -> None:
        self._running = False
        self._run_btn.state(["!disabled"])
        self._status_var.set("模擬失敗")
        messagebox.showerror("模擬失敗", str(exc))

    def _on_run_done(self, fig, stats: dict, trace: dict, cfg: GaussianFieldConfig, png_path: Path) -> None:
        for child in self._figure_holder.winfo_children():
            child.destroy()
        if self._fig_canvas is not None:
            self._fig_canvas.get_tk_widget().destroy()

        self._fig_canvas = FigureCanvasTkAgg(fig, master=self._figure_holder)
        self._fig_canvas.draw()
        self._fig_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self._steps_tree.delete(*self._steps_tree.get_children())
        for s in trace["steps"]:
            pos_text = ", ".join(f"{cfg.axis_names[i]}={v:.1f}" for i, v in enumerate(s["pos"]))
            grad_text = f"{s['grad_mag']:.4f}" if s["grad_mag"] is not None else "—"
            step_text = f"{s['step_size']:.1f}" if s["step_size"] is not None else "—"
            self._steps_tree.insert("", tk.END, values=(s["iter"], pos_text, f"{s['power_dbm']:.2f}",
                                                          grad_text, step_text))

        lines = [
            f"軸: {' '.join(cfg.axis_names)}    中心: {cfg.center.tolist()}    σ: {cfg.sigma.tolist()}",
            f"代表路徑（種子={cfg.seed}）：{'已收斂' if trace['converged'] else '未收斂'}，"
            f"共 {trace['iterations']} 步，詳見「逐步記錄」分頁",
            f"批次統計（{stats['n_trials']} 次）成功率: {stats['success_rate']:.1%}    "
            f"平均步數: {stats['iterations_mean']:.1f} ± {stats['iterations_std']:.1f}",
            f"最終誤差(歐氏距離, pulse): {stats['final_error_mean_pulse']:.1f} ± {stats['final_error_std_pulse']:.1f}",
        ]
        for axis, err in stats["final_error_per_axis_mean_pulse"].items():
            lines.append(f"  {axis} 軸平均誤差: {err:.1f} pulse")
        lines.append(f"向量圖已存至: {png_path}")

        self._stats_text.configure(state=tk.NORMAL)
        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, "\n".join(lines))
        self._stats_text.configure(state=tk.DISABLED)

        self._running = False
        self._run_btn.state(["!disabled"])
        self._status_var.set("完成")


def main() -> None:
    root = tk.Tk()
    GaussianVectorSimApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
