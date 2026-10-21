"""見本文書の設計: 4 領域 × 10 メカニズム、文型、前後に足す文。

各メカニズムは (対象, 課題, 手段, 作用, 効果) の概念 ID の組。
同じメカニズムを 3 通りの言い回しで書き（実文書の代わり）、さらに
言い換え 1 件と関係反転 1 件を作る（制御文書）。
"""
from __future__ import annotations

DOMAINS = {
    "battery": "電池熱管理",
    "surface": "表面処理",
    "robot": "ロボット制御",
    "optics": "光学センシング",
}

# (mechanism_id, domain, T, P, M, A, E)
MECHANISMS: list[tuple[str, str, str, str, str, str, str]] = [
    ("bat01", "battery", "T_module", "P_temp_rise", "M_cooling_channel", "A_heat_transfer", "E_long_life"),
    ("bat02", "battery", "T_cell", "P_temp_rise", "M_coolant_circulation", "A_heat_transfer", "E_output_stable"),
    ("bat03", "battery", "T_module", "P_thermal_runaway", "M_insulator", "A_heat_block", "E_safety"),
    ("bat04", "battery", "T_pack", "P_temp_uneven", "M_thermal_sheet", "A_heat_spread", "E_uniform_temp"),
    ("bat05", "battery", "T_cell", "P_capacity_fade", "M_temp_feedback", "A_temp_regulation", "E_long_life"),
    ("bat06", "battery", "T_module", "P_temp_rise", "M_heat_sink", "A_heat_dissipation", "E_long_life"),
    ("bat07", "battery", "T_pack", "P_condensation", "M_dehumidifier", "A_moisture_removal", "E_safety"),
    ("bat08", "battery", "T_cell", "P_thermal_runaway", "M_temp_feedback", "A_early_detection", "E_safety"),
    ("bat09", "battery", "T_module", "P_temp_uneven", "M_cooling_channel", "A_heat_spread", "E_uniform_temp"),
    ("bat10", "battery", "T_cell", "P_capacity_fade", "M_coolant_circulation", "A_heat_transfer", "E_long_life"),
    ("sur01", "surface", "T_metal_substrate", "P_corrosion", "M_anticorrosion_coating", "A_barrier", "E_durability"),
    ("sur02", "surface", "T_pipe", "P_corrosion", "M_sacrificial_anode", "A_galvanic", "E_durability"),
    ("sur03", "surface", "T_mold", "P_wear", "M_hard_plating", "A_hardening", "E_long_life"),
    ("sur04", "surface", "T_metal_substrate", "P_peeling", "M_primer", "A_adhesion", "E_durability"),
    ("sur05", "surface", "T_resin_member", "P_peeling", "M_plasma", "A_adhesion", "E_appearance"),
    ("sur06", "surface", "T_pipe", "P_fouling", "M_water_repellent", "A_low_surface_energy", "E_maintenance"),
    ("sur07", "surface", "T_mold", "P_corrosion", "M_hard_plating", "A_barrier", "E_durability"),
    ("sur08", "surface", "T_resin_member", "P_fouling", "M_water_repellent", "A_low_surface_energy", "E_appearance"),
    ("sur09", "surface", "T_metal_substrate", "P_wear", "M_dlc", "A_low_friction", "E_long_life"),
    ("sur10", "surface", "T_pipe", "P_corrosion", "M_anticorrosion_coating", "A_barrier", "E_maintenance"),
    ("rob01", "robot", "T_robot_arm", "P_vibration", "M_vibration_control", "A_disturbance_comp", "E_positioning_accuracy"),
    ("rob02", "robot", "T_robot_arm", "P_position_error", "M_learned_model", "A_trajectory_correction", "E_positioning_accuracy"),
    ("rob03", "robot", "T_robot_arm", "P_collision", "M_force_sensor", "A_force_detect", "E_safety"),
    ("rob04", "robot", "T_motor", "P_temp_rise", "M_fan", "A_heat_transfer", "E_output_stable"),
    ("rob05", "robot", "T_agv", "P_collision", "M_vision_sensor", "A_obstacle_detect", "E_safety"),
    ("rob06", "robot", "T_gripper", "P_grasp_failure", "M_force_sensor", "A_force_detect", "E_cycle_time"),
    ("rob07", "robot", "T_robot_arm", "P_vibration", "M_learned_model", "A_disturbance_comp", "E_cycle_time"),
    ("rob08", "robot", "T_motor", "P_temp_rise", "M_temp_feedback", "A_temp_regulation", "E_output_stable"),
    ("rob09", "robot", "T_agv", "P_position_error", "M_vision_sensor", "A_trajectory_correction", "E_positioning_accuracy"),
    ("rob10", "robot", "T_gripper", "P_grasp_failure", "M_learned_model", "A_trajectory_correction", "E_cycle_time"),
    ("opt01", "optics", "T_image_sensor", "P_noise", "M_peltier", "A_heat_transfer", "E_image_quality"),
    ("opt02", "optics", "T_lidar", "P_thermal_drift", "M_peltier", "A_temp_regulation", "E_measure_accuracy"),
    ("opt03", "optics", "T_lens", "P_fogging", "M_antifog", "A_hydrophilic", "E_image_quality"),
    ("opt04", "optics", "T_lens", "P_stray_light", "M_ar_coating", "A_reflection_control", "E_image_quality"),
    ("opt05", "optics", "T_lidar", "P_noise", "M_bandpass_filter", "A_light_rejection", "E_measure_accuracy"),
    ("opt06", "optics", "T_photodetector", "P_thermal_drift", "M_temp_compensation", "A_temp_dependency_comp", "E_measure_accuracy"),
    ("opt07", "optics", "T_image_sensor", "P_noise", "M_learned_model", "A_noise_removal", "E_image_quality"),
    ("opt08", "optics", "T_lidar", "P_fogging", "M_heater", "A_heat_supply", "E_measure_accuracy"),
    ("opt09", "optics", "T_photodetector", "P_temp_rise", "M_heat_sink", "A_heat_dissipation", "E_output_stable"),
    ("opt10", "optics", "T_lens", "P_fouling", "M_water_repellent", "A_low_surface_energy", "E_maintenance"),
]

# 文型。triples は (始点スロット, 述語, 終点スロット, 始点役割, 終点役割, 否定, 推量)。
# スロット T/P/M/A/E が本文の概念に入る。
Slot = tuple[str, str, str, str, str, bool, bool]

REAL_TEMPLATES: list[tuple[str, str, list[Slot]]] = [
    (
        "R1",
        "{T}では{P}が生じやすいという課題がある。そこで{M}を設け、{A}を図ることで{P}を抑制する。これにより{E}が得られる。",
        [
            ("P", "occurs_in", "T", "課題", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("M", "suppresses", "P", "手段", "課題", False, False),
            ("M", "leads_to", "E", "手段", "効果", False, False),
        ],
    ),
    (
        "R2",
        "{P}を防止するため、{T}に{M}を備える。{M}によって{A}が行われ、{P}が低減される結果、{E}を実現できる。",
        [
            ("M", "prevents", "P", "手段", "課題", False, False),
            ("M", "applied_to", "T", "手段", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("A", "suppresses", "P", "作用", "課題", False, False),
            ("A", "leads_to", "E", "作用", "効果", False, False),
        ],
    ),
    (
        "R3",
        "本技術は{T}における{P}の対策に関する。{M}を用いて{A}を行うことにより、{P}を抑えて{E}を図る構成である。",
        [
            ("P", "occurs_in", "T", "課題", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("M", "suppresses", "P", "手段", "課題", False, False),
            ("M", "leads_to", "E", "手段", "効果", False, False),
        ],
    ),
    (
        "R4",
        "{T}において{P}が問題となる。{M}を配置して{A}を実現し、{P}を軽減する。その結果、{E}が達成される。",
        [
            ("P", "occurs_in", "T", "課題", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("M", "suppresses", "P", "手段", "課題", False, False),
            ("M", "leads_to", "E", "手段", "効果", False, False),
        ],
    ),
    (
        "R5",
        "{M}を採用した{T}を提案する。{M}が{A}を担うため、{T}の{P}が抑えられ、{E}につながる。",
        [
            ("M", "applied_to", "T", "手段", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("P", "occurs_in", "T", "課題", "対象", False, False),
            ("M", "suppresses", "P", "手段", "課題", False, False),
            ("M", "leads_to", "E", "手段", "効果", False, False),
        ],
    ),
]

PARAPHRASE_TEMPLATES: list[tuple[str, str, list[Slot]]] = [
    (
        "Q1",
        "{T}で起こる{P}への対策として{M}を採用する。{M}が{A}をもたらし、{P}を減らすので、{E}につながる。",
        [
            ("P", "occurs_in", "T", "課題", "対象", False, False),
            ("M", "applied_to", "T", "手段", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("M", "suppresses", "P", "手段", "課題", False, False),
            ("M", "leads_to", "E", "手段", "効果", False, False),
        ],
    ),
    (
        "Q2",
        "{T}の{P}を解消したい。{M}を組み込めば{A}が生じ、{P}が抑え込まれる。結果として{E}が見込める。",
        [
            ("P", "occurs_in", "T", "課題", "対象", False, False),
            ("M", "performs", "A", "手段", "作用", False, False),
            ("A", "suppresses", "P", "作用", "課題", False, False),
            ("M", "leads_to", "E", "手段", "効果", False, False),
        ],
    ),
]

# 関係反転: role = 手段だった概念が課題を受ける対象になる / direction = 手段が課題を引き起こす
INVERTED_TEMPLATES: dict[str, list[tuple[str, str, list[Slot]]]] = {
    "role": [
        (
            "V1",
            "{T}に設けた{M}において{P}が生じるという課題がある。{P}は{M}を劣化させ、{A}を損なう。そのため{E}が得られにくい。",
            [
                ("M", "applied_to", "T", "対象", "対象", False, False),
                ("P", "occurs_in", "M", "課題", "対象", False, False),
                ("P", "degrades", "M", "課題", "対象", False, False),
                ("P", "suppresses", "A", "課題", "作用", False, False),
                ("A", "leads_to", "E", "作用", "効果", True, False),
            ],
        ),
        (
            "V2",
            "{M}自体の{P}が問題となる。{T}の運転中に{M}で{P}が進行すると、{A}が低下し、{E}が失われるおそれがある。",
            [
                ("P", "occurs_in", "M", "課題", "対象", False, False),
                ("P", "degrades", "M", "課題", "対象", False, False),
                ("P", "suppresses", "A", "課題", "作用", False, False),
                ("A", "leads_to", "E", "作用", "効果", True, True),
            ],
        ),
    ],
    "direction": [
        (
            "W1",
            "{T}に{M}を設けると、{A}に伴って{P}が引き起こされる。{M}は{P}を招くため、{E}が損なわれるおそれがある。",
            [
                ("M", "applied_to", "T", "手段", "対象", False, False),
                ("A", "causes", "P", "作用", "課題", False, False),
                ("M", "causes", "P", "手段", "課題", False, False),
                ("M", "leads_to", "E", "手段", "効果", True, True),
            ],
        ),
        (
            "W2",
            "{M}を用いた{T}では、{A}の副作用として{P}が増大する。つまり{M}が{P}の原因となり、{E}は得られない。",
            [
                ("M", "applied_to", "T", "手段", "対象", False, False),
                ("P", "occurs_in", "T", "課題", "対象", False, False),
                ("A", "causes", "P", "作用", "課題", False, False),
                ("M", "causes", "P", "手段", "課題", False, False),
                ("M", "leads_to", "E", "手段", "効果", True, False),
            ],
        ),
    ],
}

# 文字数を 90〜110 に合わせるための前置き文（概念語を含まない）
PREFACES: dict[str, list[str]] = {
    "battery": ["電動車両の普及が進んでいる。", "急速充電の需要が高まっている。", "蓄電容量の大型化が進んでいる。"],
    "surface": ["屋外設備の長期使用が増えている。", "部品の軽量化が求められている。", "過酷な環境での使用が増えている。"],
    "robot": ["工場の自動化が進んでいる。", "協働作業の需要が高まっている。", "多品種少量生産が広がっている。"],
    "optics": ["車載センシングの用途が広がっている。", "屋外での計測需要が高まっている。", "小型化と高感度化が求められている。"],
}
SUFFIXES: list[str] = ["量産性にも配慮した構成である。", "既存設備への適用も容易である。", "部品点数の増加も小さい。"]
