"""
多軸高斯耦光場模擬 + 梯度向量圖 + 尋峰統計

獨立工具腳本：不連接硬體、不 import main_ai.py / ds102_ctrl.py / fiber_scanner.py，
純數學模擬，用來在沒有硬體時觀察「多軸高斯耦光曲面」的梯度場長相，
以及在該場上跑一個簡化爬升搜尋所需的步數／誤差統計，作為尋光演算法調參的參考。

模型：
    P_linear(x) = P_peak_linear * exp(-Σ (x_i - c_i)^2 / (2 σ_i^2))
    P_dBm(x)    = 10 log10(P_linear(x))  （量測雜訊加在 dBm domain）
    ∇P_dBm 為解析梯度，箭頭方向即「量測到的功率上升最快方向」——這就是尋光演算法
    （coordinate descent / Powell）實際在追的向量場。

    量測下限（noise_floor_dbm）不是拿來夾住讀值的下限，而是「量測本身會失敗」的
    門檻，對應 meter_GPIB.py 的 (ok, value) 慣例與 HP 8153A 實機在 underrange 時
    回傳 sentinel（+9.9E+37）——見 read_power_dbm()。爬升搜尋在讀不到訊號時没有
    梯度可用，會退化成沿隨機方向走固定步長的「盲搜」，直到重新進入可讀範圍，
    對應真機的階段零盲搜粗掃。向量圖裡的灰色區域就是這個「讀不到」的範圍。

用法範例：
    venv/Scripts/python.exe gaussian_vector_sim.py
    venv/Scripts/python.exe gaussian_vector_sim.py --axes X Y Z --sigma 4000 6000 5000 \
        --center 1500 -800 0 --trials 200 --outdir gaussian_sim_output
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
import matplotlib
from matplotlib.figure import Figure
from matplotlib.patches import Patch

# 刻意不 import matplotlib.pyplot：這個模組同時被 CLI（存 PNG）與
# gaussian_vector_sim_gui.py（用 FigureCanvasTkAgg 直接內嵌）共用，
# 不碰 pyplot 的全域狀態機就不會有 backend 互相干擾或 Figure 洩漏的問題。
matplotlib.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

AXES = ["X", "Y", "Z", "U", "V", "W"]


@dataclass
class GaussianFieldConfig:
    axis_names: Sequence[str]
    center: np.ndarray          # 每軸尖峰位置（pulse）
    sigma: np.ndarray           # 每軸高斯寬度（pulse）
    peak_power_dbm: float = -10.0
    noise_floor_dbm: float = -60.0
    noise_std_db: float = 0.05  # 量測雜訊標準差（dB domain）
    seed: int | None = 42

    @property
    def n_axes(self) -> int:
        return len(self.axis_names)


def true_power_dbm(pos: np.ndarray, cfg: GaussianFieldConfig) -> np.ndarray:
    """理論上的真實耦光功率（無雜訊、不做量測下限判斷），pos 最後一維為軸數。"""
    peak_linear = 10 ** (cfg.peak_power_dbm / 10.0)
    exponent = -np.sum(((pos - cfg.center) ** 2) / (2 * cfg.sigma ** 2), axis=-1)
    linear = np.clip(peak_linear * np.exp(exponent), 1e-300, None)
    return 10 * np.log10(linear)


def read_power_dbm(pos: np.ndarray, cfg: GaussianFieldConfig,
                    rng: np.random.Generator | None) -> tuple[np.ndarray, np.ndarray]:
    """模擬一次量測：真實功率加雜訊後，低於 noise_floor_dbm（量測下限）視為讀不到。

    回傳 (ok, value)：ok=False 時 value 為 nan，呼叫端不可把它當成真實功率使用。
    對應 meter_GPIB.py 的 get_power() (ok, value) 慣例與 HP 8153A 實機在 underrange
    時回傳 sentinel（+9.9E+37）而非一個「看起來合理」的極低讀值——訊號太弱時，
    量測本身就是失敗的，不是量到一個很小的數字。"""
    true_dbm = true_power_dbm(pos, cfg)
    if rng is not None and cfg.noise_std_db > 0:
        noisy_dbm = true_dbm + rng.normal(0.0, cfg.noise_std_db, size=np.shape(true_dbm))
    else:
        noisy_dbm = true_dbm
    ok = noisy_dbm >= cfg.noise_floor_dbm
    value = np.where(ok, noisy_dbm, np.nan)
    return ok, value


def gradient_dbm(pos: np.ndarray, cfg: GaussianFieldConfig) -> np.ndarray:
    """解析梯度（無雜訊），形狀與 pos 相同：d(dBm)/d(x_i) = -(10/ln10) * (x_i-c_i)/σ_i^2。"""
    scale = 10.0 / math.log(10.0)
    return -scale * (pos - cfg.center) / (cfg.sigma ** 2)


def _build_grid(a_range: tuple[float, float], b_range: tuple[float, float], n: int):
    a_vals = np.linspace(a_range[0], a_range[1], n)
    b_vals = np.linspace(b_range[0], b_range[1], n)
    return np.meshgrid(a_vals, b_vals)


def build_vector_field_figure(cfg: GaussianFieldConfig, grid_range: tuple[float, float], grid_points: int,
                               fixed_values: np.ndarray, path: np.ndarray | None = None) -> Figure:
    """對每一對軸畫梯度向量圖（其餘軸固定在 fixed_values），多軸時排成子圖網格，回傳 Figure 供存檔或內嵌 GUI 共用。
    path 給定時（形狀 (n_steps, n_axes)），把該次爬升搜尋走過的每一步疊在對應軸對上，
    讓使用者實際看到每一步怎麼沿著向量場移動，不是只看最終統計。"""
    pairs = list(itertools.combinations(range(cfg.n_axes), 2))
    if not pairs:
        pairs = [(0, 0)]  # 單軸情況：退化成 1D，仍畫成一張圖以下方 1 軸函式呈現
    n_cols = min(3, len(pairs))
    n_rows = math.ceil(len(pairs) / n_cols)
    fig = Figure(figsize=(5.5 * n_cols, 4.6 * n_rows))
    axes_arr = fig.subplots(n_rows, n_cols, squeeze=False)

    for idx, (ia, ib) in enumerate(pairs):
        ax = axes_arr[idx // n_cols][idx % n_cols]
        A, B = _build_grid(grid_range, grid_range, grid_points)
        pos = np.tile(fixed_values, A.shape + (1,))
        pos[..., ia] = A
        pos[..., ib] = B

        power = true_power_dbm(pos, cfg)
        readable = power >= cfg.noise_floor_dbm
        grad = gradient_dbm(pos, cfg)
        Ga, Gb = grad[..., ia], grad[..., ib]
        mag = np.hypot(Ga, Gb)
        mag_safe = np.where(mag > 1e-12, mag, 1.0)
        # 讀不到訊號的區域（低於量測下限）不畫等高線、也不畫箭頭——
        # 真實情況下這裡連方向都拿不到，畫出來會讓人誤以為訊號可用。
        Ga_plot = np.where(readable, Ga, np.nan)
        Gb_plot = np.where(readable, Gb, np.nan)
        power_masked = np.ma.masked_where(~readable, power)

        ax.set_facecolor("#d9d9d9")
        cf = ax.contourf(A, B, power_masked, levels=20, cmap="viridis")
        ax.quiver(A, B, Ga_plot / mag_safe, Gb_plot / mag_safe, mag, cmap="autumn", scale=25, width=0.004)
        ax.plot(cfg.center[ia], cfg.center[ib], marker="*", color="white", markersize=14,
                markeredgecolor="black", label="真實尖峰")
        if path is not None:
            ax.plot(path[:, ia], path[:, ib], color="deepskyblue", linewidth=1.5, marker="o",
                    markersize=3, alpha=0.9, label="搜尋路徑")
            ax.plot(path[0, ia], path[0, ib], marker="s", color="lime", markersize=10,
                    markeredgecolor="black", label="起點")
            ax.plot(path[-1, ia], path[-1, ib], marker="X", color="red", markersize=10,
                    markeredgecolor="black", label="終點")
        ax.set_xlabel(f"{cfg.axis_names[ia]} 軸 (pulse)")
        ax.set_ylabel(f"{cfg.axis_names[ib]} 軸 (pulse)")
        ax.set_title(f"{cfg.axis_names[ia]}–{cfg.axis_names[ib]} 梯度向量圖")
        fig.colorbar(cf, ax=ax, label="功率 (dBm)")
        handles, _labels = ax.get_legend_handles_labels()
        if (~readable).any():
            handles.append(Patch(facecolor="#d9d9d9", label="讀不到（低於量測下限）"))
        ax.legend(handles=handles, fontsize=7, loc="upper right", framealpha=0.8)

    for idx in range(len(pairs), n_rows * n_cols):
        axes_arr[idx // n_cols][idx % n_cols].axis("off")

    fig.tight_layout()
    return fig


def save_vector_field_png(cfg: GaussianFieldConfig, grid_range: tuple[float, float], grid_points: int,
                           fixed_values: np.ndarray, outdir: Path) -> Path:
    fig = build_vector_field_figure(cfg, grid_range, grid_points, fixed_values)
    outdir.mkdir(parents=True, exist_ok=True)
    out_path = outdir / "gaussian_vector_field.png"
    fig.savefig(out_path, dpi=150)
    return out_path


def _reading_step(pos: np.ndarray, cfg: GaussianFieldConfig, rng: np.random.Generator,
                   n_iter: int, mode: str, grad_mag: float | None, step_size: float | None) -> tuple[bool, dict]:
    """量一次目前位置，回傳 (ok, 該步的記錄 dict)。ok=False 時 power_dbm 記為 None（讀不到）。"""
    ok, value = read_power_dbm(pos, cfg, rng)
    return bool(ok), {
        "iter": n_iter, "pos": pos.tolist(),
        "power_dbm": float(value) if ok else None,
        "readable": bool(ok), "mode": mode,
        "grad_mag": grad_mag, "step_size": step_size,
    }


def _hill_climb_one(cfg: GaussianFieldConfig, grid_range: tuple[float, float], max_iter: int,
                     tol_pulse: float, step_gain: float, rng: np.random.Generator,
                     start_pos: np.ndarray | None = None, record_steps: bool = False) -> dict:
    """單次沿梯度向量場爬升搜尋：每步先量測目前位置，量得到才用（含雜訊的）梯度方向走一步；
    量不到（低於量測下限）時沒有方向資訊可用，改成沿一個隨機方向走固定步長「盲搜」，
    直到重新進入可讀範圍——對應真機無光/訊號太弱時的 underrange 行為，不是灌一個假的低值。
    record_steps=True 時額外記錄每一步的位置／量測功率／模式／梯度大小，供軌跡疊圖與逐步記錄用。"""
    pos = start_pos.copy() if start_pos is not None else rng.uniform(grid_range[0], grid_range[1], size=cfg.n_axes)
    step = step_gain * (grid_range[1] - grid_range[0])
    converged = False
    blind_dir: np.ndarray | None = None
    n_iter = 0

    if record_steps:
        _, step0 = _reading_step(pos, cfg, rng, 0, "起點", None, None)
        steps = [step0]
    else:
        steps = None

    for n_iter in range(1, max_iter + 1):
        ok, _value = read_power_dbm(pos, cfg, rng)
        if not ok:
            if blind_dir is None:
                blind_dir = rng.normal(size=cfg.n_axes)
                blind_dir /= np.linalg.norm(blind_dir)
            pos = pos + blind_dir * step
            if record_steps:
                _, s = _reading_step(pos, cfg, rng, n_iter, "盲搜", None, float(step))
                steps.append(s)
            continue  # 盲搜階段不縮步長、不判斷收斂——沒訊號時談誤差沒有意義

        blind_dir = None
        grad = gradient_dbm(pos, cfg)
        noisy_grad = grad + rng.normal(0.0, cfg.noise_std_db, size=grad.shape) / cfg.sigma
        mag = np.linalg.norm(noisy_grad)
        if mag < 1e-9:
            break
        direction = noisy_grad / mag
        pos = pos + direction * step
        if record_steps:
            _, s = _reading_step(pos, cfg, rng, n_iter, "梯度", float(mag), float(step))
            steps.append(s)
        step *= 0.92  # 每步緩降，模擬座標下降的步長縮減
        err = float(np.linalg.norm(pos - cfg.center))
        if err < tol_pulse:
            converged = True
            break

    result = {
        "converged": converged,
        "iterations": n_iter,
        "final_error_euclid": float(np.linalg.norm(pos - cfg.center)),
        "final_error_per_axis": np.abs(pos - cfg.center).tolist(),
    }
    if record_steps:
        result["steps"] = steps
    return result


def trace_hill_climb(cfg: GaussianFieldConfig, grid_range: tuple[float, float], max_iter: int,
                      tol_pulse: float, step_gain: float, rng: np.random.Generator,
                      start_pos: np.ndarray | None = None) -> dict:
    """跑一次帶完整步進記錄的爬升搜尋，給 GUI 疊圖與「看得到每一步」的逐步表格用。"""
    return _hill_climb_one(cfg, grid_range, max_iter, tol_pulse, step_gain, rng,
                            start_pos=start_pos, record_steps=True)


def simulate_hill_climb(cfg: GaussianFieldConfig, grid_range: tuple[float, float], n_trials: int,
                         max_iter: int, tol_pulse: float, step_gain: float, rng: np.random.Generator) -> dict:
    """跑 n_trials 次爬升搜尋，統計成功率／步數／誤差（不記錄逐步軌跡，效能考量）。"""
    results = [
        _hill_climb_one(cfg, grid_range, max_iter, tol_pulse, step_gain, rng)
        for _ in range(n_trials)
    ]

    n = len(results)
    success = sum(r["converged"] for r in results)
    iters = np.array([r["iterations"] for r in results])
    errs = np.array([r["final_error_euclid"] for r in results])
    per_axis_err = np.array([r["final_error_per_axis"] for r in results])

    return {
        "n_trials": n,
        "success_rate": success / n,
        "iterations_mean": float(iters.mean()),
        "iterations_std": float(iters.std()),
        "final_error_mean_pulse": float(errs.mean()),
        "final_error_std_pulse": float(errs.std()),
        "final_error_per_axis_mean_pulse": {
            name: float(per_axis_err[:, i].mean()) for i, name in enumerate(cfg.axis_names)
        },
    }


def run(cfg: GaussianFieldConfig, grid_range: tuple[float, float], grid_points: int,
        n_trials: int, max_iter: int, tol_pulse: float, step_gain: float, outdir: Path) -> dict:
    trace_rng = np.random.default_rng(cfg.seed)
    trace = trace_hill_climb(cfg, grid_range, max_iter, tol_pulse, step_gain, trace_rng)
    path = np.array([s["pos"] for s in trace["steps"]])

    fig = build_vector_field_figure(cfg, grid_range, grid_points, fixed_values=cfg.center, path=path)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_path = outdir / "gaussian_vector_field.png"
    fig.savefig(fig_path, dpi=150)

    steps_path = outdir / "gaussian_vector_sim_steps.json"
    steps_path.write_text(json.dumps(trace["steps"], ensure_ascii=False, indent=2), encoding="utf-8")

    batch_rng = np.random.default_rng(cfg.seed + 1)
    stats = simulate_hill_climb(cfg, grid_range, n_trials, max_iter, tol_pulse, step_gain, batch_rng)

    summary = {
        "config": {
            "axes": list(cfg.axis_names),
            "center": cfg.center.tolist(),
            "sigma": cfg.sigma.tolist(),
            "peak_power_dbm": cfg.peak_power_dbm,
            "noise_floor_dbm": cfg.noise_floor_dbm,
            "noise_std_db": cfg.noise_std_db,
            "grid_range": list(grid_range),
            "grid_points": grid_points,
            "seed": cfg.seed,
        },
        "vector_field_png": str(fig_path),
        "traced_run_steps_json": str(steps_path),
        "traced_run_converged": trace["converged"],
        "traced_run_iterations": trace["iterations"],
        "hill_climb_stats": stats,
    }
    summary_path = outdir / "gaussian_vector_sim_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_json"] = str(summary_path)
    return summary


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--axes", nargs="+", default=["X", "Y"], choices=AXES, help="要模擬的軸（AXES 子集，順序即繪圖順序）")
    p.add_argument("--center", nargs="+", type=float, default=None, help="每軸尖峰位置 pulse，數量需與 --axes 相同；預設全 0")
    p.add_argument("--sigma", nargs="+", type=float, default=None, help="每軸高斯寬度 pulse，數量需與 --axes 相同；預設全 5000")
    p.add_argument("--peak-power-dbm", type=float, default=-10.0)
    p.add_argument("--noise-floor-dbm", type=float, default=-60.0)
    p.add_argument("--noise-std-db", type=float, default=0.05)
    p.add_argument("--grid-range", nargs=2, type=float, default=[-15000.0, 15000.0], metavar=("MIN", "MAX"))
    p.add_argument("--grid-points", type=int, default=25)
    p.add_argument("--trials", type=int, default=200, help="爬升搜尋模擬次數，統計成功率／收斂步數用")
    p.add_argument("--max-iter", type=int, default=200)
    p.add_argument("--tol-pulse", type=float, default=50.0, help="視為收斂的誤差門檻（pulse）")
    p.add_argument("--step-gain", type=float, default=0.05, help="初始步長 = step_gain * (grid_range 寬度)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--outdir", type=Path, default=Path("gaussian_sim_output"))
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    n = len(args.axes)
    center = np.array(args.center if args.center is not None else [0.0] * n, dtype=float)
    sigma = np.array(args.sigma if args.sigma is not None else [5000.0] * n, dtype=float)
    if len(center) != n or len(sigma) != n:
        raise SystemExit(f"--center/--sigma 數量必須與 --axes（{n} 軸）相同")

    cfg = GaussianFieldConfig(
        axis_names=args.axes, center=center, sigma=sigma,
        peak_power_dbm=args.peak_power_dbm, noise_floor_dbm=args.noise_floor_dbm,
        noise_std_db=args.noise_std_db, seed=args.seed,
    )
    summary = run(
        cfg, grid_range=tuple(args.grid_range), grid_points=args.grid_points,
        n_trials=args.trials, max_iter=args.max_iter, tol_pulse=args.tol_pulse,
        step_gain=args.step_gain, outdir=args.outdir,
    )

    stats = summary["hill_climb_stats"]
    print(f"向量圖已存至: {summary['vector_field_png']}（含代表路徑的每一步疊圖）")
    print(f"逐步記錄已存至: {summary['traced_run_steps_json']}")
    print(f"統計摘要已存至: {summary['summary_json']}")
    print(f"成功率: {stats['success_rate']:.1%}  平均步數: {stats['iterations_mean']:.1f} ± {stats['iterations_std']:.1f}")
    print(f"最終誤差(歐氏距離, pulse): {stats['final_error_mean_pulse']:.1f} ± {stats['final_error_std_pulse']:.1f}")
    for axis, err in stats["final_error_per_axis_mean_pulse"].items():
        print(f"  {axis} 軸平均誤差: {err:.1f} pulse")


if __name__ == "__main__":
    main()
