"""
多軸高斯耦光場模擬 + 梯度向量圖 + 尋峰統計

獨立工具腳本：不連接硬體、不 import main_ai.py / ds102_ctrl.py / fiber_scanner.py，
純數學模擬，用來在沒有硬體時觀察「多軸高斯耦光曲面」的梯度場長相，
以及在該場上跑一個簡化爬升搜尋所需的步數／誤差統計，作為尋光演算法調參的參考。

模型：
    P_linear(x) = P_peak_linear * exp(-Σ (x_i - c_i)^2 / (2 σ_i^2))
    P_dBm(x)    = 10 log10(P_linear(x))  （量測雜訊加在 dBm domain）
    真實梯度 ∇P_dBm 只用來畫向量圖背景（見 gradient_dbm()）；搜尋演算法本身
    用有限差分探測估計方向（見 estimate_gradient_dbm()），不偷看解析梯度。

    量測下限（noise_floor_dbm）不是拿來夾住讀值的下限，而是「量測本身會失敗」的
    門檻，對應 meter_GPIB.py 的 (ok, value) 慣例與 HP 8153A 實機在 underrange 時
    回傳 sentinel（+9.9E+37）——見 read_power_dbm()。爬升搜尋在讀不到訊號時沒有
    梯度可用，會退化成方形螺旋（多軸）或正負交替遞增（單軸）系統性地找訊號，
    對應真機的階段零盲搜粗掃；找到後每軸各自用 Rprop 風格自適應步長爬升（同號
    加大、反號減半），不會被單一全域衰減排程拖累收斂快慢不同的軸。向量圖裡的
    灰色區域就是「讀不到」的範圍。

    grid_range／start_range 是兩件事：grid_range 只給畫圖用，start_range（預設
    沿用 grid_range）決定起點取樣與步長尺度。單模光纖這類「可讀半徑（幾十 pulse）
    遠小於滑台全行程（上萬 pulse）」的場景務必分開設定，否則步長會用全行程尺度
    算出來、比可讀半徑大上好幾倍，盲搜螺旋直接跨過訊號區而漏掉。

用法範例：
    venv/Scripts/python.exe gaussian_vector_sim.py
    venv/Scripts/python.exe gaussian_vector_sim.py --axes X Y Z --sigma 4000 6000 5000 \
        --center 1500 -800 0 --trials 200 --outdir gaussian_sim_output

    單模光纖三軸實測校準場景（0.2um/pulse，9umx9um 耦光截面，X/Y/Z 各自全行程，
    已於對話中驗證：成功率 98~100%、各軸誤差 0.1~2.8 pulse，見對話紀錄）：
        venv/Scripts/python.exe gaussian_vector_sim.py --axes X Y Z \
            --center 5300 5250 20500 --sigma 22.5 22.5 250 \
            --grid-range 0 10600 0 10500 0 41000 \
            --start-range 4500 6100 4450 6050 19700 21300 \
            --step-gain 0.035 --tol-pulse 5 --max-iter 3500 --trials 300
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


def normalize_axis_ranges(grid_range, n_axes: int) -> np.ndarray:
    """把 grid_range 統一成形狀 (n_axes, 2) 的每軸獨立範圍。

    grid_range 可以是單一 (min,max)（向後相容，廣播到所有軸——舊呼叫方式跟這次
    改動前完全一樣），也可以是每軸各自一組 (min,max) 的序列。真實三軸滑台常常
    行程尺度差很多（例如 Z 軸單向對焦行程遠比 X/Y 橫向對準行程長），共用同一組
    範圍會讓較小的軸搜尋範圍失真、或較大的軸範圍不夠，所以兩種輸入都要支援。"""
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
    path 給定時（形狀 (n_steps, n_axes)），把該次爬升搜尋走過的每一步疊在對應軸對上，
    讓使用者實際看到每一步怎麼沿著向量場移動，不是只看最終統計。"""
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


def _square_spiral_deltas(budget: int) -> list[tuple[int, int]]:
    """方形螺旋的單軸位移序列（軸 0／軸 1 交替），每個元素是 (axis_idx, ±1 個 step)。
    邊長序列 1,1,2,2,3,3,…，跟 fiber_scanner.py 的 run_stage0_blind 同一套幾何
    （DS102 只有單軸相對步進、沒有插補，斜線畫不出來，所以每步只能動一軸）。
    只沿第一、第二軸展開；第三軸以後（若有）在盲搜期間維持不動。"""
    directions = [(0, 1), (1, 1), (0, -1), (1, -1)]  # +axis0, +axis1, -axis0, -axis1
    deltas: list[tuple[int, int]] = []
    leg_length = 1
    dir_idx = 0
    while len(deltas) < budget:
        for _ in range(2):  # 每個邊長值連續用兩段（右→上、左→下…）才會長出正方形
            axis_idx, sign = directions[dir_idx % 4]
            for _ in range(leg_length):
                deltas.append((axis_idx, sign))
                if len(deltas) >= budget:
                    return deltas
            dir_idx += 1
        leg_length += 1
    return deltas


def _expanding_bounce_deltas(budget: int) -> list[tuple[int, int]]:
    """單軸版的「盲搜」：沒有第二軸可以展開螺旋時，沿軸 0 正負交替、距離遞增地找，
    覆蓋 ±1,±2,±3,… 個 step，跟方形螺旋一樣保證涵蓋、不會漏掉任何整數格點。"""
    deltas: list[tuple[int, int]] = []
    n = 1
    sign = 1
    while len(deltas) < budget:
        for _ in range(n):
            deltas.append((0, sign))
            if len(deltas) >= budget:
                return deltas
        sign *= -1
        n += 1
    return deltas


def estimate_gradient_dbm(pos: np.ndarray, cfg: GaussianFieldConfig, rng: np.random.Generator,
                           probe_step) -> tuple[np.ndarray, int]:
    """有限差分估計梯度：每一軸做一次中央差分量測（pos±probe_step[i]），量到才用。

    probe_step 可以是純量（所有軸共用同一探測距離）或每軸各自一個值的陣列——
    軸的物理尺度差很多時（例如某軸行程是另一軸的好幾倍），每軸用自己尺度算出來
    的探測距離才有意義，共用一個值要嘛對小尺度軸太粗、要嘛對大尺度軸太細。

    這是刻意取代 gradient_dbm() 解析梯度的地方——真實滑台演算法（fiber_scanner.py
    的座標下降）沒有解析梯度可用，只能靠實際移動＋量測去估計方向，這裡讓模擬照做：
    每一軸的方向分量都要付出真正的探測讀值（也可能讀不到，共用 read_power_dbm）。
    單邊讀不到時退化成單邊差分（多打一次中心點）；兩邊都讀不到就把該軸分量記 0
    （這一軸暫時沒有方向資訊，不是「沒有梯度」，下一次呼叫會重新探測）。
    回傳 (grad_estimate, probes_used)：probes_used 是這次估計實際用掉的量測次數，
    供呼叫端統計「探測成本」。"""
    probe_step_arr = np.broadcast_to(probe_step, (cfg.n_axes,))
    grad = np.zeros(cfg.n_axes)
    probes = 0
    center_ok: bool | None = None
    center_val = 0.0
    for i in range(cfg.n_axes):
        h = probe_step_arr[i]
        pos_plus = pos.copy()
        pos_plus[i] += h
        pos_minus = pos.copy()
        pos_minus[i] -= h
        ok_plus, val_plus = read_power_dbm(pos_plus, cfg, rng)
        ok_minus, val_minus = read_power_dbm(pos_minus, cfg, rng)
        probes += 2
        if ok_plus and ok_minus:
            grad[i] = (val_plus - val_minus) / (2 * h)
        elif ok_plus or ok_minus:
            if center_ok is None:
                center_ok, center_val = read_power_dbm(pos, cfg, rng)
                probes += 1
            if center_ok:
                if ok_plus:
                    grad[i] = (val_plus - center_val) / h
                else:
                    grad[i] = (center_val - val_minus) / h
            # 中心點也讀不到：這一軸沒有可用資訊，grad[i] 維持 0
        # 兩側都讀不到：這一軸沒有可用資訊，grad[i] 維持 0
    return grad, probes


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
                     start_pos: np.ndarray | None = None, record_steps: bool = False,
                     start_range=None) -> dict:
    """單次沿梯度向量場爬升搜尋：每步先量測目前位置，量得到才用有限差分（見
    estimate_gradient_dbm()）估計出的方向走一步——不是解析梯度，真實滑台演算法
    （fiber_scanner.py 的座標下降）本來就拿不到解析梯度，只能靠實際探測。
    量不到目前位置、或探測完全沒有方向資訊時，改成方形螺旋（多軸時）或正負交替
    遞增（單軸時）系統性地找訊號——跟 fiber_scanner.py 的 run_stage0_blind 同一套
    幾何，保證涵蓋、不會像隨機方向那樣可能永遠走偏。找到訊號後螺旋作廢，回到梯度爬升。

    探測距離（probe_step）刻意跟移動步長（step）分開、不隨迭代縮小：有限差分的
    兩個探測點量測雜訊固定是 noise_std_db，探測距離變小時「訊號（功率差）」跟著
    變小、但雜訊不變，會讓方向估計的訊噪比隨探測距離線性崩壞——探測距離縮到跟
    雜訊同量級時，估出來的方向已經是雜訊而不是真正的梯度。這是典型的「有限差分
    步長越小、雜訊放大越嚴重」問題，固定探測距離（用起始步長）才能維持穩定的
    方向估計品質；移動步長仍然照常縮小，只影響「沿著這個方向走多遠」，兩者分開。
    grid_range 可以是單一 (min,max)（所有軸共用）或每軸各自一組 (min,max)（形狀
    (n_axes,2)）——見 normalize_axis_ranges()。軸的物理行程尺度差很多時（例如
    某軸行程是另一軸的好幾倍）務必用後者，否則起點取樣與步長都會被錯誤尺度帶偏。

    step／probe_step 都是「每軸各自一個值」的向量，不是共用一個純量：移動距離
    =方向分量×該軸自己的步長，等於用每軸的行程尺度做對角線預條件（diagonal
    preconditioning）——尺度大的軸（例如 Z 軸行程遠比 X/Y 長）自然走比較大步，
    不會被尺度小的軸拖著只走一點點，也不會讓尺度小的軸被尺度大的步長沖過頭。
    start_range（預設 None，沿用 grid_range）：起點取樣與步長計算改用這個範圍，
    grid_range 則只留給呼叫端畫圖使用。兩者刻意分開：單模光纖的耦光可讀半徑
    （幾十 pulse 量級）遠小於滑台整段機械行程（可能上萬 pulse），步長若用全行程
    寬度算出來，會比可讀半徑大上好幾倍，方形螺旋直接跨過訊號區而漏掉（真機同一個
    教訓見 docs/fiber-scan.md〈格距必須小於耦合光斑的尺度〉）。start_range 對應
    真機的「使用者已粗略對準、盲搜半徑相對起點設定」（blind_max_radius 的概念），
    不是要盲搜掃過整段行程。
    record_steps=True 時額外記錄每一步的位置／量測功率／模式／梯度大小，供軌跡疊圖與逐步記錄用。"""
    ranges = normalize_axis_ranges(grid_range, cfg.n_axes)
    start_ranges = ranges if start_range is None else normalize_axis_ranges(start_range, cfg.n_axes)
    pos = start_pos.copy() if start_pos is not None else rng.uniform(start_ranges[:, 0], start_ranges[:, 1])
    step_max = step_gain * (start_ranges[:, 1] - start_ranges[:, 0])  # 每軸步長上限（見下方成長/縮小說明）
    step = step_max.copy()  # 每軸各自的移動步長（向量）
    probe_step = step_max.copy()  # 探測距離固定在起始步長，不隨 step 縮小（見上方 docstring）
    prev_sign: np.ndarray | None = None
    converged = False
    blind_deltas: list[tuple[int, int]] | None = None
    blind_idx = 0
    n_iter = 0
    total_probes = 0

    if record_steps:
        _, step0 = _reading_step(pos, cfg, rng, 0, "起點", None, None)
        steps = [step0]
    else:
        steps = None

    for n_iter in range(1, max_iter + 1):
        ok, _value = read_power_dbm(pos, cfg, rng)
        mag = 0.0
        direction = None
        if ok:
            grad_est, probes = estimate_gradient_dbm(pos, cfg, rng, probe_step)
            total_probes += probes
            mag = float(np.linalg.norm(grad_est))
            if mag >= 1e-9:
                direction = grad_est / mag

        if direction is None:
            # 目前位置讀不到、或探測完全沒抓到方向：兩種情況都沒有梯度可用，走盲搜。
            if blind_deltas is None:
                remaining = max_iter - n_iter + 1
                blind_deltas = (_square_spiral_deltas(remaining) if cfg.n_axes >= 2
                                 else _expanding_bounce_deltas(remaining))
                blind_idx = 0
            if blind_idx < len(blind_deltas):
                axis_idx, sign = blind_deltas[blind_idx]
                blind_idx += 1
                pos = pos.copy()
                pos[axis_idx] += sign * step[axis_idx]  # 每軸用自己的步長，不是共用一個純量
            if record_steps:
                moved = float(step[axis_idx]) if blind_idx > 0 else None
                _, s = _reading_step(pos, cfg, rng, n_iter, "盲搜", None, moved)
                steps.append(s)
            continue  # 盲搜階段不縮步長、不判斷收斂——沒訊號時談誤差沒有意義

        blind_deltas = None
        move = direction * step  # 每軸位移＝方向分量 × 該軸自己的步長（見上方 docstring）
        pos = pos + move
        if record_steps:
            _, s = _reading_step(pos, cfg, rng, n_iter, "梯度", mag, float(np.linalg.norm(move)))
            steps.append(s)

        # 每軸各自的自適應步長（Rprop 風格），取代單一全域衰減率：
        # 全軸共用同一個「每步 ×0.92」排程時，梯度陡的軸（訊噪比高，例如 σ 小的
        # 橫向軸）幾步就逼近終點、方向開始來回擺盪；梯度緩的軸（σ 大的軸，例如
        # 縱向對焦）需要走更多步才能縮短同樣的相對距離。共用排程縮到後段所有軸
        # 步長都已經逼近 0，還沒收斂的軸從此再也動不了、永遠卡在當下的誤差——
        # 這裡改成方向連續同號（還在朝同一邊前進）就放大步長，反號（已經跨過
        # 終點、開始擺盪）才縮小，讓每一軸依自己的收斂進度各自決定何時該減速，
        # 上限夾在 step_max 避免暴衝。
        cur_sign = np.sign(direction)
        if prev_sign is not None:
            same = (cur_sign == prev_sign) & (cur_sign != 0)
            flipped = (cur_sign != prev_sign) & (prev_sign != 0)
            step = np.where(flipped, step * 0.5, np.where(same, step * 1.1, step))
            step = np.minimum(step, step_max)
        prev_sign = cur_sign
        err = float(np.linalg.norm(pos - cfg.center))
        if err < tol_pulse:
            converged = True
            break

    result = {
        "converged": converged,
        "iterations": n_iter,
        "total_probes": total_probes,
        "final_error_euclid": float(np.linalg.norm(pos - cfg.center)),
        "final_error_per_axis": np.abs(pos - cfg.center).tolist(),
    }
    if record_steps:
        result["steps"] = steps
    return result


def trace_hill_climb(cfg: GaussianFieldConfig, grid_range: tuple[float, float], max_iter: int,
                      tol_pulse: float, step_gain: float, rng: np.random.Generator,
                      start_pos: np.ndarray | None = None, start_range=None) -> dict:
    """跑一次帶完整步進記錄的爬升搜尋，給 GUI 疊圖與「看得到每一步」的逐步表格用。
    start_range 見 _hill_climb_one()：預設沿用 grid_range，只在單模光纖等「可讀
    半徑遠小於全行程」的場景才需要另外指定。"""
    return _hill_climb_one(cfg, grid_range, max_iter, tol_pulse, step_gain, rng,
                            start_pos=start_pos, record_steps=True, start_range=start_range)


def simulate_hill_climb(cfg: GaussianFieldConfig, grid_range: tuple[float, float], n_trials: int,
                         max_iter: int, tol_pulse: float, step_gain: float, rng: np.random.Generator,
                         start_range=None) -> dict:
    """跑 n_trials 次爬升搜尋，統計成功率／步數／誤差（不記錄逐步軌跡，效能考量）。
    start_range 見 _hill_climb_one()。"""
    results = [
        _hill_climb_one(cfg, grid_range, max_iter, tol_pulse, step_gain, rng, start_range=start_range)
        for _ in range(n_trials)
    ]

    n = len(results)
    success = sum(r["converged"] for r in results)
    iters = np.array([r["iterations"] for r in results])
    probes = np.array([r["total_probes"] for r in results])
    errs = np.array([r["final_error_euclid"] for r in results])
    per_axis_err = np.array([r["final_error_per_axis"] for r in results])

    return {
        "n_trials": n,
        "success_rate": success / n,
        "iterations_mean": float(iters.mean()),
        "iterations_std": float(iters.std()),
        "probes_mean": float(probes.mean()),
        "probes_std": float(probes.std()),
        "final_error_mean_pulse": float(errs.mean()),
        "final_error_std_pulse": float(errs.std()),
        "final_error_per_axis_mean_pulse": {
            name: float(per_axis_err[:, i].mean()) for i, name in enumerate(cfg.axis_names)
        },
    }


def run(cfg: GaussianFieldConfig, grid_range: tuple[float, float], grid_points: int,
        n_trials: int, max_iter: int, tol_pulse: float, step_gain: float, outdir: Path,
        start_range=None) -> dict:
    trace_rng = np.random.default_rng(cfg.seed)
    trace = trace_hill_climb(cfg, grid_range, max_iter, tol_pulse, step_gain, trace_rng, start_range=start_range)
    path = np.array([s["pos"] for s in trace["steps"]])

    fig = build_vector_field_figure(cfg, grid_range, grid_points, fixed_values=cfg.center, path=path)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_path = outdir / "gaussian_vector_field.png"
    fig.savefig(fig_path, dpi=150)

    steps_path = outdir / "gaussian_vector_sim_steps.json"
    steps_path.write_text(json.dumps(trace["steps"], ensure_ascii=False, indent=2), encoding="utf-8")

    batch_rng = np.random.default_rng(cfg.seed + 1)
    stats = simulate_hill_climb(cfg, grid_range, n_trials, max_iter, tol_pulse, step_gain, batch_rng,
                                 start_range=start_range)

    summary = {
        "config": {
            "axes": list(cfg.axis_names),
            "center": cfg.center.tolist(),
            "sigma": cfg.sigma.tolist(),
            "peak_power_dbm": cfg.peak_power_dbm,
            "noise_floor_dbm": cfg.noise_floor_dbm,
            "noise_std_db": cfg.noise_std_db,
            "grid_range": normalize_axis_ranges(grid_range, cfg.n_axes).tolist(),
            "start_range": (normalize_axis_ranges(start_range, cfg.n_axes).tolist()
                             if start_range is not None else None),
            "grid_points": grid_points,
            "seed": cfg.seed,
        },
        "vector_field_png": str(fig_path),
        "traced_run_steps_json": str(steps_path),
        "traced_run_converged": trace["converged"],
        "traced_run_iterations": trace["iterations"],
        "traced_run_probes": trace["total_probes"],
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
    p.add_argument("--grid-range", nargs="+", type=float, default=[-15000.0, 15000.0],
                    help="繪圖範圍（沒給 --start-range 時起點取樣也用這個）。給 2 個值＝所有軸"
                         "共用；給 2×軸數個值＝每軸各自一組 (min,max)，順序對應 --axes")
    p.add_argument("--start-range", nargs="+", type=float, default=None,
                    help="起點取樣與步長計算改用這個範圍（不給就沿用 --grid-range）。"
                         "單模光纖等可讀半徑遠小於全行程的場景要用這個——對應真機"
                         "「使用者已粗略對準、盲搜半徑相對起點設定」，不是盲搜掃過整段行程；"
                         "格式同 --grid-range")
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
        n_trials=args.trials, max_iter=args.max_iter, tol_pulse=args.tol_pulse,
        step_gain=args.step_gain, outdir=args.outdir, start_range=start_range,
    )

    stats = summary["hill_climb_stats"]
    print(f"向量圖已存至: {summary['vector_field_png']}（含代表路徑的每一步疊圖）")
    print(f"逐步記錄已存至: {summary['traced_run_steps_json']}")
    print(f"統計摘要已存至: {summary['summary_json']}")
    print(f"成功率: {stats['success_rate']:.1%}  平均步數: {stats['iterations_mean']:.1f} ± {stats['iterations_std']:.1f}")
    print(f"平均探測次數(有限差分梯度估計的量測成本): {stats['probes_mean']:.1f} ± {stats['probes_std']:.1f}")
    print(f"最終誤差(歐氏距離, pulse): {stats['final_error_mean_pulse']:.1f} ± {stats['final_error_std_pulse']:.1f}")
    for axis, err in stats["final_error_per_axis_mean_pulse"].items():
        print(f"  {axis} 軸平均誤差: {err:.1f} pulse")


if __name__ == "__main__":
    main()
