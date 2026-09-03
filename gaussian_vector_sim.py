"""
多軸高斯耦光場模擬 + 梯度向量圖 + fiber_scanner.py 尋光演算法忠實重現

獨立工具腳本：不連接硬體、不 import main_ai.py / ds102_ctrl.py，純數學模擬，
用來在沒有硬體時觀察「多軸高斯耦光曲面」的梯度場長相，以及在該場上跑
fiber_scanner.py 階段零（盲搜粗掃）＋階段一（座標下降）演算法的忠實重現版本，
統計成功率／步數／探測次數，作為調參與行為驗證的參考。

🔴 這裡的搜尋邏輯是刻意逐行對照 fiber_scanner.py 的 run_stage0_blind() /
run_stage1() / _search_axis_once() / _rect_spiral_offsets() 移植的，不是另外
設計的通用演算法——早期版本（steepest descent + 有限差分梯度 + Rprop 自適應
步長）跟真實座標下降是兩套不同的演算法，在這個場上調出來的參數與結論不能
直接套用回 fiber_scanner.py，這一版修正了這個落差。移植對照表：

    fiber_scanner.py                          gaussian_vector_sim.py
    ────────────────────────────────────────  ──────────────────────────────
    _rect_spiral_offsets()                    rect_spiral_offsets()
    run_stage0_blind()                        run_stage0_blind()
    _search_axis_once()                       search_axis_once()
    run_stage1()                               run_stage1()
    calibrate_noise() / _noise_floor()        calibrate_noise() / noise floor
    _move_relative() / _targets_reachable()   move_relative()（travel_bounds）
    _move_multi_axis()                        move_multi()
    get_power() 的 (ok, value) 慣例            read_power_dbm() 的 (ok, value)

模型：
    P_linear(x) = P_peak_linear * exp(-Σ (x_i - c_i)^2 / (2 σ_i^2))
    P_dBm(x)    = 10 log10(P_linear(x))  （量測雜訊加在 dBm domain）
    真實梯度 ∇P_dBm 只用來畫向量圖背景（見 gradient_dbm()）；搜尋演算法本身
    跟真機一樣，完全不碰解析梯度，只靠實際「移動＋量測」的座標下降。

    量測下限（noise_floor_dbm）不是拿來夾住讀值的下限，而是「量測本身會失敗」的
    門檻，對應 meter_GPIB.py 的 (ok, value) 慣例與 HP 8153A 實機在 underrange 時
    回傳 sentinel（+9.9E+37）——見 read_power_dbm()。

    travel_bounds／start_range 是兩件事：travel_bounds 是搜尋演算法實際受限的
    行程範圍（對應真機 `_travel_bounds`／滑台實體行程），影響 move_relative()
    是否成功；grid_range 只給畫圖用。單模光纖「可讀半徑遠小於全行程」的場景，
    盲搜半徑（blind_max_radius）務必遠小於 travel_bounds 的寬度，否則格距
    （blind_step）比耦光光斑還粗，螺旋會直接跨過訊號區而漏掉——對應
    docs/fiber-scan.md〈格距必須小於耦合光斑的尺度〉的同一個教訓。

用法範例：
    venv/Scripts/python.exe gaussian_vector_sim.py
    venv/Scripts/python.exe gaussian_vector_sim.py --axes X Y Z --sigma 4000 6000 5000 \
        --center 1500 -800 0 --trials 200 --outdir gaussian_sim_output

    單模光纖三軸實測校準場景（0.2um/pulse，9umx9um 耦光截面）：
        venv/Scripts/python.exe gaussian_vector_sim.py --axes X Y Z \
            --center 5300 5250 20500 --sigma 22.5 22.5 250 \
            --grid-range 0 10600 0 10500 0 41000 \
            --start-range 4500 6100 4450 6050 19700 21300 \
            --blind-step 20 --blind-max-radius 800 --tol-pulse 5 --trials 300
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import matplotlib
from matplotlib.figure import Figure
from matplotlib.patches import Patch

# 刻意不 import matplotlib.pyplot：本模組同時被 CLI（存 PNG）與
# gaussian_vector_sim_gui.py（FigureCanvasTkAgg 內嵌）共用，不碰 pyplot
# 的全域狀態機就不會有 backend 互相干擾或 Figure 洩漏的問題。
matplotlib.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

AXES = ["X", "Y", "Z", "U", "V", "W"]

# ---- 以下數值對照 fiber_scanner.py 的同名常數，保持一致（起跳用的
# 保守預設，不是校準值）----
DEFAULT_STEP_MIN = 2
DEFAULT_MAX_CYCLES = 5
DEFAULT_NOISE_SIGMA_MULT = 3.0
REOPEN_STEP_MULT = 8
STAGE1_MAX_PASSES_PER_STEP = 200
DEFAULT_BLIND_STEP = 200
DEFAULT_BLIND_MAX_RADIUS = 1000
DEFAULT_BLIND_SIGNAL_SIGMA_MULT = 5.0
DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB = 0.5
DEFAULT_CALIBRATE_NOISE_SAMPLES = 5


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


def measure(pos: np.ndarray, cfg: GaussianFieldConfig, rng: np.random.Generator) -> tuple[bool, float | None]:
    """單點量測的純量版本，對應 fiber_scanner.py 的 _measure_here()／get_power()。"""
    ok, value = read_power_dbm(pos, cfg, rng)
    return bool(ok), (float(value) if ok else None)


def gradient_dbm(pos: np.ndarray, cfg: GaussianFieldConfig) -> np.ndarray:
    """解析梯度（無雜訊），只給畫向量圖背景用——搜尋演算法本身不使用，
    形狀與 pos 相同：d(dBm)/d(x_i) = -(10/ln10) * (x_i-c_i)/σ_i^2。"""
    scale = 10.0 / math.log(10.0)
    return -scale * (pos - cfg.center) / (cfg.sigma ** 2)


def normalize_axis_ranges(grid_range, n_axes: int) -> np.ndarray:
    """把 grid_range 統一成形狀 (n_axes, 2) 的每軸獨立範圍。

    grid_range 可以是單一 (min,max)（向後相容，廣播到所有軸），也可以是每軸
    各自一組 (min,max) 的序列。真實三軸滑台常常行程尺度差很多（例如 Z 軸單向
    對焦行程遠比 X/Y 橫向對準行程長），共用同一組範圍會讓較小的軸搜尋範圍
    失真、或較大的軸範圍不夠，所以兩種輸入都要支援。"""
    arr = np.asarray(grid_range, dtype=float)
    if arr.shape == (2,):
        return np.tile(arr, (n_axes, 1))
    if arr.shape == (n_axes, 2):
        return arr
    raise ValueError(f"grid_range 形狀必須是 (2,)（所有軸共用）或 ({n_axes}, 2)（每軸各自），"
                      f"實際收到 {arr.shape}")


def _build_grid(a_range: tuple[float, float], b_range: tuple[float, float], n: int):
    a_vals = np.linspace(a_range[0], a_range[1], n)
    b_vals = np.linspace(b_range[0], b_range[1], n)
    return np.meshgrid(a_vals, b_vals)


def build_vector_field_figure(cfg: GaussianFieldConfig, grid_range: tuple[float, float], grid_points: int,
                               fixed_values: np.ndarray, path: np.ndarray | None = None) -> Figure:
    """對每一對軸畫梯度向量圖（其餘軸固定在 fixed_values），多軸時排成子圖網格，回傳 Figure 供存檔或內嵌 GUI 共用。
    path 給定時（形狀 (n_steps, n_axes)），把該次搜尋走過的每一步疊在對應軸對上，
    讓使用者實際看到每一步怎麼移動，不是只看最終統計。"""
    ranges = normalize_axis_ranges(grid_range, cfg.n_axes)
    pairs = list(itertools.combinations(range(cfg.n_axes), 2))
    if not pairs:
        pairs = [(0, 0)]  # 單軸情況：退化成 1D，仍畫成一張圖以下方 1 軸函式呈現
    n_cols = min(3, len(pairs))
    n_rows = math.ceil(len(pairs) / n_cols)
    fig = Figure(figsize=(5.5 * n_cols, 4.6 * n_rows))
    axes_arr = fig.subplots(n_rows, n_cols, squeeze=False)

    for idx, (ia, ib) in enumerate(pairs):
        ax = axes_arr[idx // n_cols][idx % n_cols]
        A, B = _build_grid(ranges[ia], ranges[ib], grid_points)
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


# ======================================================================
# 以下忠實對照 fiber_scanner.py 的搜尋演算法（見檔案開頭的移植對照表）
# ======================================================================

def rect_spiral_offsets(step: int, max_radius: int):
    """方形螺旋位移序列，逐行對照 fiber_scanner.py 的 _rect_spiral_offsets()：
    由中心往外、逐環擴大，邊長序列 1,1,2,2,3,3,...，每步只動一軸（對應 DS102
    單軸相對步進、沒有插補的限制，畫不出阿基米德螺旋的斜線）。"""
    yield (0, 0)
    step = max(1, int(step))
    n_rings = int(max_radius) // step
    if n_rings < 1:
        return
    dirs = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    a = b = 0
    d = 0
    leg_len = 1
    while leg_len <= 2 * n_rings + 1:
        for _ in range(2):
            da, db = dirs[d % 4]
            for _ in range(leg_len):
                a += da * step
                b += db * step
                if max(abs(a), abs(b)) <= max_radius:
                    yield (a, b)
            d += 1
        leg_len += 1


def move_relative(pos: np.ndarray, axis_idx: int, delta: float,
                   bounds: np.ndarray) -> tuple[bool, np.ndarray]:
    """單軸相對移動，對照 fiber_scanner.py 的 _move_relative()＋_targets_reachable()：
    超出 bounds（對應真機的 _travel_bounds／軟體限位）視為撞限位/逾時，回傳
    (False, 原 pos)，不移動。"""
    if delta == 0:
        return True, pos
    target = pos[axis_idx] + delta
    lo, hi = bounds[axis_idx]
    if target < lo or target > hi:
        return False, pos
    new_pos = pos.copy()
    new_pos[axis_idx] = target
    return True, new_pos


def move_multi(pos: np.ndarray, deltas: dict[int, float], bounds: np.ndarray) -> tuple[bool, np.ndarray]:
    """多軸同時出發，對照 fiber_scanner.py 的 _move_multi_axis()：任一軸超界
    就整批失敗、完全不移動（真機是「任一軸撞限位就整批 STOP」的連坐設計）。"""
    new_pos = pos.copy()
    for axis_idx, delta in deltas.items():
        target = pos[axis_idx] + delta
        lo, hi = bounds[axis_idx]
        if target < lo or target > hi:
            return False, pos
        new_pos[axis_idx] = target
    return True, new_pos


def calibrate_noise(pos: np.ndarray, cfg: GaussianFieldConfig, rng: np.random.Generator,
                     n_samples: int = DEFAULT_CALIBRATE_NOISE_SAMPLES) -> tuple[float | None, float]:
    """在目前位置重複量測，估計基準功率與雜訊標準差，對照
    fiber_scanner.py 的 calibrate_noise()。一個有效讀值都拿不到時回傳
    (None, 0.0)（真機同一情境：baseline 留 None，呼叫端須改用退化判準或中止）。"""
    powers = []
    for _ in range(max(2, n_samples)):
        ok, p = measure(pos, cfg, rng)
        if ok:
            powers.append(p)
    if not powers:
        return None, 0.0
    baseline = sum(powers) / len(powers)
    if len(powers) < 2:
        return baseline, 0.0
    var = sum((p - baseline) ** 2 for p in powers) / (len(powers) - 1)
    return baseline, var ** 0.5


def search_axis_once(pos: np.ndarray, axis_idx: int, step: int, cfg: GaussianFieldConfig,
                      rng: np.random.Generator, bounds: np.ndarray,
                      floor: float) -> tuple[bool, float | None, np.ndarray, int]:
    """單軸尋峰子程序跑一輪，逐行對照 fiber_scanner.py 的 _search_axis_once()：
    方向探測（帶雜訊門檻，不是赤裸 `>`）→ 定向爬坡 → 三點拋物線內插。

    回傳 (converged, p_curr, new_pos, probes)。converged=True 代表這個步長
    已收斂（起點量不到、或兩側都沒有超過雜訊門檻的改善）；False 代表位置有
    移動，這個步長值得在新位置再跑一輪（呼叫端的 while 迴圈負責重複呼叫，
    直到收斂才縮步——跟真機一致，不是這裡自己判斷要不要縮步）。"""
    probes = 0
    ok0, p0 = measure(pos, cfg, rng)
    probes += 1
    if p0 is None:
        return True, None, pos, probes  # 起點都量不到，無從比較

    moved_plus, pos_plus = move_relative(pos, axis_idx, step, bounds)
    p_plus = None
    if moved_plus:
        _, p_plus = measure(pos_plus, cfg, rng)
        probes += 1

    if moved_plus and p_plus is not None and p_plus > p0 + floor:
        direction, p_prev, p_curr, pos = 1, p0, p_plus, pos_plus
    else:
        moved_minus, pos_minus = move_relative(pos, axis_idx, -step, bounds)
        p_minus = None
        if moved_minus:
            _, p_minus = measure(pos_minus, cfg, rng)
            probes += 1
        if moved_minus and p_minus is not None and p_minus > p0 + floor:
            direction, p_prev, p_curr, pos = -1, p0, p_minus, pos_minus
        else:
            return True, p0, pos, probes  # 兩側都沒有可用的改善 → 此步長已收斂

    # 定向爬坡：固定步長沿方向走，直到不再上升或撞限位
    p_next: float | None = None
    while True:
        moved, pos_try = move_relative(pos, axis_idx, direction * step, bounds)
        if not moved:
            p_next = None
            break
        _, cur_power = measure(pos_try, cfg, rng)
        probes += 1
        if cur_power is None or cur_power <= p_curr + floor:
            p_next = cur_power
            break  # 沒有更好，留在目前峰值點（pos 不採用 pos_try）
        pos = pos_try
        p_prev, p_curr = p_curr, cur_power

    # 三點拋物線內插（只有三點都有效才做）
    if p_next is not None:
        denom = p_prev - 2 * p_curr + p_next
        if abs(denom) > floor:
            offset = 0.5 * (p_prev - p_next) / denom  # 介於約 -0.5~0.5 個 step
            x_peak = round(offset * step)
            if x_peak != 0:
                moved, pos_try = move_relative(pos, axis_idx, direction * x_peak, bounds)
                if moved:
                    _, p_try = measure(pos_try, cfg, rng)
                    probes += 1
                    if p_try is not None and p_try > p_curr + floor:
                        p_curr, pos = p_try, pos_try
                    # 內插沒有實際更好，維持在格點最大值（不採用 pos_try）

    return False, p_curr, pos, probes


def run_stage1(pos: np.ndarray, active_axis_idxs: list[int], initial_step: dict[int, int],
               cfg: GaussianFieldConfig, rng: np.random.Generator, bounds: np.ndarray, floor: float,
               step_min: int = DEFAULT_STEP_MIN, max_cycles: int = DEFAULT_MAX_CYCLES,
               reopen_step_mult: int = REOPEN_STEP_MULT,
               max_passes_per_step: int = STAGE1_MAX_PASSES_PER_STEP,
               record_steps: bool = False) -> dict:
    """階段一：座標下降式全域粗定位，逐行對照 fiber_scanner.py 的 run_stage1()。
    對每一個實際搜尋軸，依 initial_step[axis] 起跳，反覆「尋峰→步長減半」，
    直到步長縮到 step_min。多輪 cycle 處理軸間耦合：第 1 輪用完整起始步長做
    真正的全域粗掃，第 2 輪起從 step_min×reopen_step_mult 重新收斂。"""
    steps = [] if record_steps else None
    total_probes = 0

    for cycle in range(1, max_cycles + 1):
        if cycle == 1:
            step_by_axis = {ax: max(step_min, int(initial_step.get(ax, step_min * 8)))
                             for ax in active_axis_idxs}
        else:
            step_by_axis = {ax: step_min * reopen_step_mult for ax in active_axis_idxs}

        total_improvement = 0.0
        for ax in active_axis_idxs:
            _, p_before = measure(pos, cfg, rng)
            total_probes += 1
            s = step_by_axis[ax]
            passes = 0
            while s >= step_min:
                converged, p_curr, pos, probes = search_axis_once(pos, ax, s, cfg, rng, bounds, floor)
                total_probes += probes
                if record_steps:
                    steps.append({
                        "phase": "座標下降", "cycle": cycle, "axis": cfg.axis_names[ax],
                        "pos": pos.tolist(), "power_dbm": p_curr, "readable": p_curr is not None,
                        "step_size": s,
                    })
                if converged:
                    s //= 2
                    passes = 0
                    continue
                passes += 1
                if passes >= max_passes_per_step:
                    # 保險絲熔斷（對照 STAGE1_MAX_PASSES_PER_STEP 的用法）：
                    # 這個步長附近沒有可靠梯度（多半是純雜訊），強制縮步繼續。
                    s //= 2
                    passes = 0
            _, p_after = measure(pos, cfg, rng)
            total_probes += 1
            if p_before is not None and p_after is not None:
                total_improvement += max(0.0, p_after - p_before)

        if total_improvement < floor:
            break

    return {"pos": pos, "probes": total_probes, "steps": steps}


def run_stage0_blind(pos: np.ndarray, ax_a: int, ax_b: int, blind_step: int, blind_max_radius: int,
                      cfg: GaussianFieldConfig, rng: np.random.Generator, bounds: np.ndarray,
                      target_power: float | None, record_steps: bool = False) -> dict:
    """階段零：盲搜粗掃，逐行對照 fiber_scanner.py 的 run_stage0_blind()。
    在 ax_a-ax_b 平面上用方形螺旋由內而外掃描，找到第一個「功率達到
    target_power 門檻」的格點就停下並回傳 found=True；target_power=None
    時退化成「掃到任何一個讀得到的有效值就算找到」（對應真機起點底噪低於
    儀器可量測下限、每次讀值都是 sentinel 的情境）。

    🔴 每個格點都用「絕對目標－目前座標」重算 delta，不是沿螺旋累加相對
    位移——撞限位跳過的格點不會讓後續格點整體平移，見 move_multi()。"""
    steps = [] if record_steps else None
    probes = 0
    center_pos = pos.copy()
    found = False

    for idx, (da, db) in enumerate(rect_spiral_offsets(blind_step, blind_max_radius)):
        target_a = center_pos[ax_a] + da
        target_b = center_pos[ax_b] + db
        lo_a, hi_a = bounds[ax_a]
        lo_b, hi_b = bounds[ax_b]
        if not (lo_a <= target_a <= hi_a and lo_b <= target_b <= hi_b):
            continue

        deltas = {}
        if target_a != pos[ax_a]:
            deltas[ax_a] = target_a - pos[ax_a]
        if target_b != pos[ax_b]:
            deltas[ax_b] = target_b - pos[ax_b]
        if deltas:
            moved, new_pos = move_multi(pos, deltas, bounds)
            if not moved:
                continue
            pos = new_pos

        ok, power = measure(pos, cfg, rng)
        probes += 1
        if record_steps:
            steps.append({
                "phase": "盲搜", "cycle": None, "axis": None,
                "pos": pos.tolist(), "power_dbm": power, "readable": ok, "step_size": blind_step,
            })
        if ok and power is not None and (target_power is None or power >= target_power):
            found = True
            break

    return {"found": found, "pos": pos, "probes": probes, "steps": steps}


def run_real_algorithm_trial(
    cfg: GaussianFieldConfig, travel_bounds, rng: np.random.Generator,
    start_pos: np.ndarray | None = None, initial_step: dict[int, int] | None = None,
    step_min: int = DEFAULT_STEP_MIN, max_cycles: int = DEFAULT_MAX_CYCLES,
    reopen_step_mult: int = REOPEN_STEP_MULT, max_passes_per_step: int = STAGE1_MAX_PASSES_PER_STEP,
    noise_sigma_mult: float = DEFAULT_NOISE_SIGMA_MULT,
    blind_step: int = DEFAULT_BLIND_STEP, blind_max_radius: int = DEFAULT_BLIND_MAX_RADIUS,
    blind_signal_sigma_mult: float = DEFAULT_BLIND_SIGNAL_SIGMA_MULT,
    blind_signal_min_delta_db: float = DEFAULT_BLIND_SIGNAL_MIN_DELTA_DB,
    calibrate_noise_samples: int = DEFAULT_CALIBRATE_NOISE_SAMPLES,
    tol_pulse: float = 5.0, record_steps: bool = False,
) -> dict:
    """跑一次完整的階段零(視情況)+階段一，對照 fiber_scanner.py 的 run()
    在 blind_mode="auto" 下的主流程：起點校準雜訊 → 讀不到就先盲搜 → 跑階段一
    → 若最終功率仍沒有明顯高於起點基準，再盲搜一次接著重跑階段一。

    travel_bounds：搜尋演算法實際受限的行程範圍（真機的 _travel_bounds），
    決定 move_relative()／move_multi() 會不會成功；跟只給畫圖用的 grid_range
    是兩件事。盲搜只涵蓋 travel_bounds 的前兩軸（對照真機「固定 2 軸」的
    既有設計：第三軸用同樣密度掃會讓格點數變成立方，光纖對準的格距估算
    根本跑不完，見 fiber_scanner.py 的 __init__ 註解）。

    tol_pulse 不是演算法本身的收斂判準（真機沒有「已知峰值位置」可以比較），
    純粹是這裡拿來對照模擬用的「已知真值」算出最終誤差、供統計成功率用。"""
    bounds = normalize_axis_ranges(travel_bounds, cfg.n_axes)
    pos = start_pos.copy() if start_pos is not None else rng.uniform(bounds[:, 0], bounds[:, 1])
    if initial_step is None:
        widths = bounds[:, 1] - bounds[:, 0]
        initial_step = {i: max(step_min, int(0.05 * widths[i])) for i in range(cfg.n_axes)}
    else:
        initial_step = {i: max(step_min, int(v)) for i, v in initial_step.items()}

    all_steps = [] if record_steps else None
    total_probes = 0
    blind_runs = 0

    def _record(phase_steps):
        if record_steps and phase_steps:
            all_steps.extend(phase_steps)

    def _target_power(baseline, sigma):
        if baseline is None:
            return None
        delta = max(blind_signal_sigma_mult * sigma, blind_signal_min_delta_db)
        return baseline + delta

    # 起點雜訊校準：baseline／target 只在這裡（移動前）算一次，後面用來判斷
    # 階段一跑完後的功率是否明顯高於出發點。刻意不在階段一跑完後用當下位置
    # 重新校準再互比——同一位置的重複量測，差異只是雜訊，比較沒有意義
    # （對照 fiber_scanner.py 的 _power_clearly_above_baseline()）。
    baseline, sigma = calibrate_noise(pos, cfg, rng, calibrate_noise_samples)
    total_probes += calibrate_noise_samples
    floor = noise_sigma_mult * sigma if baseline is not None else 0.0
    target = _target_power(baseline, sigma)

    ax_a, ax_b = 0, 1  # 對照 _blind_axis_pair()：預設用搜尋軸的前兩軸

    # 進階段一之前，只有「起點完全讀不到」才需要先盲搜（對照 fiber_scanner.py
    # 的 run()）。起點讀得到就直接跑階段一——那本身就是從任意起點爬到峰值用的。
    if cfg.n_axes >= 2 and baseline is None:
        blind = run_stage0_blind(pos, ax_a, ax_b, blind_step, blind_max_radius, cfg, rng, bounds,
                                  target, record_steps=record_steps)
        total_probes += blind["probes"]
        blind_runs += 1
        pos = blind["pos"]
        _record(blind["steps"])
        baseline, sigma = calibrate_noise(pos, cfg, rng, calibrate_noise_samples)
        total_probes += calibrate_noise_samples
        floor = noise_sigma_mult * sigma if baseline is not None else 0.0
        target = _target_power(baseline, sigma)

    active_axis_idxs = list(range(cfg.n_axes))
    stage1 = run_stage1(pos, active_axis_idxs, initial_step, cfg, rng, bounds, floor,
                         step_min=step_min, max_cycles=max_cycles, reopen_step_mult=reopen_step_mult,
                         max_passes_per_step=max_passes_per_step, record_steps=record_steps)
    pos = stage1["pos"]
    total_probes += stage1["probes"]
    _record(stage1["steps"])

    # 階段一跑完後，拿當下功率去跟出發前的 baseline／target 比較（不重新
    # 校準）——明顯更好即確認訊號；不夠好才回頭盲搜一次再重跑階段一。
    ok_final, power_final = measure(pos, cfg, rng)
    total_probes += 1
    confirmed = ok_final and power_final is not None and (target is None or power_final >= target)

    if cfg.n_axes >= 2 and not confirmed and blind_runs < 1:
        blind = run_stage0_blind(pos, ax_a, ax_b, blind_step, blind_max_radius, cfg, rng, bounds,
                                  target, record_steps=record_steps)
        total_probes += blind["probes"]
        blind_runs += 1
        pos = blind["pos"]
        _record(blind["steps"])
        baseline, sigma = calibrate_noise(pos, cfg, rng, calibrate_noise_samples)
        total_probes += calibrate_noise_samples
        floor = noise_sigma_mult * sigma if baseline is not None else 0.0
        stage1b = run_stage1(pos, active_axis_idxs, initial_step, cfg, rng, bounds, floor,
                              step_min=step_min, max_cycles=max_cycles, reopen_step_mult=reopen_step_mult,
                              max_passes_per_step=max_passes_per_step, record_steps=record_steps)
        pos = stage1b["pos"]
        total_probes += stage1b["probes"]
        _record(stage1b["steps"])

    final_error = np.abs(pos - cfg.center)
    result = {
        "converged": bool(np.linalg.norm(pos - cfg.center) < tol_pulse),
        "blind_runs": blind_runs,
        "total_probes": total_probes,
        "final_error_euclid": float(np.linalg.norm(pos - cfg.center)),
        "final_error_per_axis": final_error.tolist(),
        "final_pos": pos.tolist(),
    }
    if record_steps:
        result["steps"] = all_steps
    return result


def trace_search(cfg: GaussianFieldConfig, travel_bounds, rng: np.random.Generator, **kwargs) -> dict:
    """跑一次帶完整步進記錄的搜尋，給 GUI 疊圖與「看得到每一步」的逐步表格用。"""
    kwargs.pop("record_steps", None)
    return run_real_algorithm_trial(cfg, travel_bounds, rng, record_steps=True, **kwargs)


def simulate_search(cfg: GaussianFieldConfig, travel_bounds, n_trials: int,
                     rng: np.random.Generator, **kwargs) -> dict:
    """跑 n_trials 次搜尋，統計成功率／探測次數／誤差（不記錄逐步軌跡，效能考量）。"""
    kwargs.pop("record_steps", None)
    results = [run_real_algorithm_trial(cfg, travel_bounds, rng, record_steps=False, **kwargs)
               for _ in range(n_trials)]

    n = len(results)
    success = sum(r["converged"] for r in results)
    probes = np.array([r["total_probes"] for r in results])
    blind_runs = np.array([r["blind_runs"] for r in results])
    errs = np.array([r["final_error_euclid"] for r in results])
    per_axis_err = np.array([r["final_error_per_axis"] for r in results])

    return {
        "n_trials": n,
        "success_rate": success / n,
        "probes_mean": float(probes.mean()),
        "probes_std": float(probes.std()),
        "blind_runs_mean": float(blind_runs.mean()),
        "final_error_mean_pulse": float(errs.mean()),
        "final_error_std_pulse": float(errs.std()),
        "final_error_per_axis_mean_pulse": {
            name: float(per_axis_err[:, i].mean()) for i, name in enumerate(cfg.axis_names)
        },
    }


def run(cfg: GaussianFieldConfig, grid_range: tuple[float, float], grid_points: int,
        n_trials: int, outdir: Path, travel_bounds=None, **kwargs) -> dict:
    bounds = travel_bounds if travel_bounds is not None else grid_range
    trace_rng = np.random.default_rng(cfg.seed)
    trace = trace_search(cfg, bounds, trace_rng, **kwargs)
    path = np.array([s["pos"] for s in trace["steps"]]) if trace.get("steps") else None

    fig = build_vector_field_figure(cfg, grid_range, grid_points, fixed_values=cfg.center, path=path)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_path = outdir / "gaussian_vector_field.png"
    fig.savefig(fig_path, dpi=150)

    steps_path = outdir / "gaussian_vector_sim_steps.json"
    steps_path.write_text(json.dumps(trace["steps"], ensure_ascii=False, indent=2), encoding="utf-8")

    batch_rng = np.random.default_rng(cfg.seed + 1)
    stats = simulate_search(cfg, bounds, n_trials, batch_rng, **kwargs)

    summary = {
        "config": {
            "axes": list(cfg.axis_names),
            "center": cfg.center.tolist(),
            "sigma": cfg.sigma.tolist(),
            "peak_power_dbm": cfg.peak_power_dbm,
            "noise_floor_dbm": cfg.noise_floor_dbm,
            "noise_std_db": cfg.noise_std_db,
            "grid_range": normalize_axis_ranges(grid_range, cfg.n_axes).tolist(),
            "travel_bounds": normalize_axis_ranges(bounds, cfg.n_axes).tolist(),
            "grid_points": grid_points,
            "seed": cfg.seed,
        },
        "vector_field_png": str(fig_path),
        "traced_run_steps_json": str(steps_path),
        "traced_run_converged": trace["converged"],
        "traced_run_probes": trace["total_probes"],
        "traced_run_blind_runs": trace["blind_runs"],
        "search_stats": stats,
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
    p.add_argument("--grid-range", nargs="+", type=float, default=[-15000.0, 15000.0],
                    help="繪圖範圍（沒給 --start-range 時搜尋行程也用這個）。給 2 個值＝所有軸"
                         "共用；給 2×軸數個值＝每軸各自一組 (min,max)，順序對應 --axes")
    p.add_argument("--start-range", nargs="+", type=float, default=None,
                    help="搜尋演算法實際受限的行程範圍（travel_bounds，對應真機 _travel_bounds），"
                         "也是起點取樣範圍（不給就沿用 --grid-range）。格式同 --grid-range")
    p.add_argument("--grid-points", type=int, default=25)
    p.add_argument("--trials", type=int, default=200, help="搜尋模擬次數，統計成功率／探測次數用")
    p.add_argument("--step-min", type=int, default=DEFAULT_STEP_MIN)
    p.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
    p.add_argument("--noise-sigma-mult", type=float, default=DEFAULT_NOISE_SIGMA_MULT)
    p.add_argument("--blind-step", type=int, default=DEFAULT_BLIND_STEP)
    p.add_argument("--blind-max-radius", type=int, default=DEFAULT_BLIND_MAX_RADIUS)
    p.add_argument("--tol-pulse", type=float, default=5.0, help="拿來對照真值算成功率的誤差門檻（pulse），不是演算法本身的收斂判準")
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

    def _parse_range(values, flag_name):
        if values is None:
            return None
        if len(values) not in (2, 2 * n):
            raise SystemExit(f"{flag_name} 必須給 2 個值（所有軸共用）或 {2 * n} 個值（每軸各自一組），"
                              f"實際給了 {len(values)} 個")
        arr = np.array(values, dtype=float)
        return arr if len(values) == 2 else arr.reshape(n, 2)

    grid_range = _parse_range(args.grid_range, "--grid-range")
    start_range = _parse_range(args.start_range, "--start-range")

    cfg = GaussianFieldConfig(
        axis_names=args.axes, center=center, sigma=sigma,
        peak_power_dbm=args.peak_power_dbm, noise_floor_dbm=args.noise_floor_dbm,
        noise_std_db=args.noise_std_db, seed=args.seed,
    )
    summary = run(
        cfg, grid_range=grid_range, grid_points=args.grid_points,
        n_trials=args.trials, outdir=args.outdir, travel_bounds=start_range,
        step_min=args.step_min, max_cycles=args.max_cycles, noise_sigma_mult=args.noise_sigma_mult,
        blind_step=args.blind_step, blind_max_radius=args.blind_max_radius, tol_pulse=args.tol_pulse,
    )

    stats = summary["search_stats"]
    print(f"向量圖已存至: {summary['vector_field_png']}（含代表路徑的每一步疊圖）")
    print(f"逐步記錄已存至: {summary['traced_run_steps_json']}")
    print(f"統計摘要已存至: {summary['summary_json']}")
    print(f"成功率: {stats['success_rate']:.1%}  平均盲搜次數: {stats['blind_runs_mean']:.2f}")
    print(f"平均探測次數: {stats['probes_mean']:.1f} ± {stats['probes_std']:.1f}")
    print(f"最終誤差(歐氏距離, pulse): {stats['final_error_mean_pulse']:.1f} ± {stats['final_error_std_pulse']:.1f}")
    for axis, err in stats["final_error_per_axis_mean_pulse"].items():
        print(f"  {axis} 軸平均誤差: {err:.1f} pulse")


if __name__ == "__main__":
    main()
