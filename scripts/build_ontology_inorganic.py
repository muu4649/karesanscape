"""無機材料の国内特許 1,200 件だけから、課題・解決手段・用途のオントロジー（inorg-1.0）を作る。

使い方（ontomap/ で実行）:
    .venv/bin/python scripts/build_ontology_inorganic.py candidates   # 開発用の半分から候補語を数える
    .venv/bin/python scripts/build_ontology_inorganic.py build        # YAML を書き出す
    .venv/bin/python scripts/build_ontology_inorganic.py validate     # 読み込み・類似度・出現率を測る
    .venv/bin/python scripts/build_ontology_inorganic.py all          # 上の 3 つを順に

決まり:
- 入力は datasets/inorganic1200/documents_sections.csv だけ。MOF のデータ（datasets/mof*、runs/、
  config/ontology_mof_v0_1.yaml、scripts/gen_ontology_mof.py）は読まない。
- doc_id の番号が奇数の文書を開発用、偶数の文書を確認用にする。候補語の抽出と、
  語を足すための「拾えなかった文書」の一覧は開発用だけで作る。確認用は出現率を測るだけ。
- 階層と別名は人が決め、下の TREE に持つ。候補語の表（candidates）を見て決めた。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ontomap.ontology import Ontology  # noqa: E402
from ontomap.patents.data import load_patent_csv  # noqa: E402
from ontomap.patents.extract import SectionExtractor  # noqa: E402

CSV = ROOT / "datasets/inorganic1200/documents_sections.csv"
OUT_YAML = ROOT / "config/ontology_inorganic_v1.yaml"
WORK = ROOT / "datasets/inorganic1200/_work/ontology_build"
VERSION = "inorg-1.0"
FROZEN_ON = "2026-10-19"
SECTIONS = {"problem": "P", "solution": "M", "use": "U"}

# ---------------------------------------------------------------------------
# 階層と別名（人が決めたデータ）
#   1 行 = 1 概念。「ID: 表示名 | 別名, 別名, ...」。字下げ 2 文字で 1 段下。
#   最上段は上位型（P/M/U）。別名の無い行は中間概念（本文照合には使わない）。
#   末端概念の表示名も照合に使うので、表示名は本文に出る言い方にする。
#   ハイフンを含む別名は、- − ‐ の 3 通りと、ハイフン無しを自動で足す。
# ---------------------------------------------------------------------------
TREE = """
P: 課題
  P_sorption: 吸着・分離性能の課題
    P_adsorption_capacity: 吸着性能 | 吸着能, 吸着能力, 吸着容量, 吸着量, 吸着特性, 吸着効率, 吸着力, 吸着性, 吸着除去性能, 吸着した量, 吸着しやす, 吸着しにく
    P_removal: 除去性能 | 除去率, 除去効率, 除去能力, 浄化能力, 消臭性能, 脱臭性能, 消臭効果, 脱臭効果, 捕捉性能, 吸着除去能, 捕集能, 捕集性能, 捕集効率
    P_selectivity: 選択性 | 分離性能, 分離係数, 分離比, 分離能, 分離効率, 選択吸着性, 選択的吸着, 選択的に吸着, 透過分離性能, 分離特性, 分離精度, 選択透過性
    P_permeance: 透過性能 | 透過速度, 透過量, 透過流束, 透過度, パーミアンス, 透過係数, 透過率, 透過性, 透水性, 通液性
    P_regeneration: 再生性 | 再生効率, 脱着性, 脱離性, 再生温度, 再生処理, 繰り返し使用, 繰返し使用, 再利用性, 脱着性能
    P_sorption_kinetics: 吸着速度 | 拡散速度, 物質移動, 拡散抵抗, 吸着平衡, 拡散性
  P_catalysis: 触媒性能の課題
    P_catalytic_activity: 触媒活性 | 触媒性能, 触媒特性, 触媒能, 光触媒活性, 浄化性能, 浄化率, 浄化効率, 反応活性, 低温活性, 活性の向上, 高活性, 触媒効率, 転化率, 反応速度, 反応効率, NOx浄化率, 浄化活性, 比活性, 光触媒機能, 触媒機能
    P_yield_selectivity: 収率 | 選択率, 生成効率, 目的生成物, 副生成物, 副生物, 反応選択性, 生成量, 転化効率
    P_deactivation: 失活 | 被毒, 触媒劣化, 活性低下, 活性の低下, 活性が低下, シンタリング, コーキング, 炭素析出, コーク, 硫黄被毒, 熱劣化, 触媒寿命
  P_electrochem: 電気化学特性の課題
    P_capacity: 放電容量 | 充電容量, 電池容量, 高容量, 充放電容量, 比容量, 容量密度, エネルギー密度, 初期容量, 静電容量, 体積容量, 重量容量, 高エネルギー密度, 電気容量, 容量, エネルギ密度, エネルギー保持率, エネルギ保持率
    P_cycle: サイクル特性 | サイクル寿命, 容量維持率, 寿命特性, 充放電サイクル, サイクル劣化, 容量劣化, 容量低下, 容量の低下, 維持率, サイクル安定性, 長寿命, 保存特性, 高温保存, サイクル性能
    P_rate: 出力特性 | レート特性, 入出力特性, 急速充電, 高出力, 内部抵抗, 抵抗上昇, 直流抵抗, レート性能, 入力特性, 出力密度, 大電流, 界面抵抗, 反応抵抗, 抵抗の上昇, パワー性能, 低温特性, 低温性能, 低温出力, 低温での
    P_conductivity: イオン伝導度 | イオン伝導性, リチウムイオン伝導度, リチウムイオン伝導性, 電子伝導性, 導電性, 電気伝導度, 電気伝導性, 伝導率, 電気抵抗, 抵抗率, 体積抵抗, 電子伝導度, プロトン伝導, 低抵抗, 抵抗値
    P_efficiency_electro: 充放電効率 | クーロン効率, 初回効率, 初期効率, 不可逆容量, 初回充放電効率, 電流効率
    P_side_reaction: 副反応 | ガス発生, 電解液の分解, 電解液と反応, デンドライト, リチウム析出, 金属の溶出, 遷移金属の溶出, 自己放電, 硫化水素の発生, 分解反応
    P_voltage: 作動電圧 | 高電圧, 高電位, 作動電位, 放電電圧, 平均電圧, 電圧降下, 過電圧, 電位窓, 耐電圧
  P_stability: 安定性・耐久性の課題
    P_thermal_stability: 耐熱性 | 熱安定性, 熱的安定性, 高温安定性, 耐熱温度, 熱的に安定, 熱に対する安定, 高温耐久性, 耐熱性能
    P_water_stability: 耐水性 | 水熱安定性, 耐水熱性, 水熱耐久性, 耐湿性, 耐湿, 吸湿, 吸水, 潮解, 水分による劣化, 水蒸気存在下, 耐水熱, 耐水蒸気性
    P_chemical_durability: 化学的安定性 | 耐薬品性, 耐酸性, 耐アルカリ性, 耐食性, 腐食, 耐酸化性, 酸化劣化, 溶出, 大気安定性, 化学的耐久性, 耐溶剤性, 化学安定性
    P_durability: 耐久性 | 寿命, 経時劣化, 長期安定性, 劣化, 経時変化, 経時的, 長期間, 長期使用, 安定性, 信頼性, 不安定
    P_structural_stability: 構造安定性 | 構造崩壊, 結晶構造の崩壊, 構造の崩壊, 骨格の崩壊, 相転移, 体積変化, 膨張, 膨張収縮, 収縮, 変形, 反り, 寸法安定性, 寸法変化, 体積膨張
  P_mechanical: 機械的特性の課題
    P_strength: 機械的強度 | 強度, 圧壊強度, 曲げ強度, 破壊強度, 高強度, 硬度, 靱性, 靭性, 破壊靱性, 破壊靭性, 耐摩耗性, 耐衝撃性, 摩耗, 圧縮強度, 機械的特性, 強固
    P_fracture: クラック | 割れ, 亀裂, 破損, 破壊, 欠け, 剥離, 粉化, 損傷, 脱落, 微粉化, 崩壊, 割れ目, 粉落ち
    P_thermal_shock: 耐熱衝撃性 | 熱衝撃, 熱膨張, 熱膨張係数, 熱応力, 熱膨張率
    P_adhesion: 密着性 | 接着性, 結着性, 結着力, 接合強度, 剥離強度, 密着力, 接着強度, 接合性
  P_structure_quality: 構造・品質の課題
    P_flow_resistance: 圧力損失 | 目詰まり, 閉塞, 通気抵抗, 圧損, 細孔閉塞, 流通抵抗, 細孔の閉塞, 塞が
    P_uniformity: 均一性 | ばらつき, 不均一, 偏り, 再現性, 均質性, 分散性, 凝集, 粗大化, 偏析, 品質のばらつき, 均一に, 均質に, ムラ, 分散しにく, 均一な, 均一である, 均一性の, 欠陥, 品質, 高品質
    P_purity: 不純物 | 高純度, 純度, 金属不純物, 残留, 着色, 異物, 混入, 不純物の混入, 残存, 夾雑物, 副生
    P_structure_target: 高比表面積 | 比表面積が大き, 比表面積の大き, 高い比表面積, 細孔が発達, 細孔容積が大き, 大きな比表面積, 多孔化, 高結晶性, 結晶性の高い, 微細化, 高多孔率, 高気孔率, 細孔が同時に発達, 発達した細孔, 細孔径を制御, 比表面積を制御, 細孔構造を制御, 細孔径の制御
  P_process: 製造上の課題
    P_cost: コスト | 高価, 安価, 低コスト, 高コスト, 製造コスト, 費用, 経済的, 経済性, 原料コスト, 処理コスト, 生産コスト, 低廉, コストダウン, 製造費用, 価格
    P_productivity: 生産性 | 製造効率, 効率的, 効率よく, 効率良く, 歩留まり, 歩留り, 量産, 大量生産, 工業的, 大規模, スケールアップ, 収量, 工業規模, 大量に, 生産効率, 高効率, 処理量, 規模拡大
    P_complexity: 煩雑 | 複雑, 工程数, 簡便, 簡易, 簡単, 手間, 長時間, 時間を要, 短時間, 多段階, 簡略, 複雑化, 部品点数, 厳しい条件, 過酷な条件, 穏和な条件, 温和な条件, 容易な条件
    P_energy_consumption: エネルギー消費 | 消費エネルギー, 省エネルギー, 消費電力, 多大なエネルギー, エネルギー効率, 省エネ, エネルギーを消費, エネルギーコスト, 高温を要, 運転コスト
    P_handling: 成形性 | 取り扱い, 取扱い, ハンドリング, 加工性, 塗工性, 粉塵, 飛散, 流動性, 成型性, 作業性, 発塵, 操作性, 微粉, 流出
  P_functional_property: 機能物性の課題
    P_thermal_property: 熱伝導率 | 熱伝導性, 断熱性, 断熱性能, 放熱性, 熱伝導度, 熱抵抗, 蓄熱, 断熱効果, 熱容量, 誘導加熱, 発熱特性
    P_optical_property: 透明性 | 透光性, 屈折率, 反射率, 光吸収, 可視光応答, 可視光, 紫外線, 光学特性, 遮光性, 白色度, 色調
    P_electrical_property: 絶縁性 | 誘電率, 誘電損失, 誘電特性, 圧電, 磁気特性, 半導体特性, 電気特性, 電気的特性
    P_size_weight: 軽量化 | 小型化, 薄型化, 軽量, 小型, 薄膜化, コンパクト
  P_env_safety: 環境・安全の課題
    P_environment: 環境負荷 | 環境汚染, 環境への負荷, 廃棄物, 廃液, 排水, 二酸化炭素排出, CO2排出, 温室効果ガス, 地球温暖化, 環境保全, 環境問題, 産業廃棄物, 廃棄, 持続可能
    P_safety: 安全性 | 発火, 熱暴走, 爆発, 毒性, 危険, 可燃性, 引火, 人体, 健康被害, 安全, 火災, 延焼, 難燃, 燃焼性, 不燃
    P_resource: 資源 | 希少, レアメタル, 枯渇, 有効利用, 資源の有効, 希少金属, 未利用, 化石燃料, 化石資源, 石油資源
  P_pollutant: 除去対象の物質
    P_nox: 窒素酸化物 | NOx, 亜酸化窒素, N2O, 一酸化窒素, 二酸化窒素, 二酸化窒素ガス
    P_sox: 硫黄酸化物 | SOx, 硫化水素, 硫黄化合物, メルカプタン, 二酸化硫黄, 硫黄分
    P_odor_voc: 揮発性有機化合物 | VOC, ホルムアルデヒド, アセトアルデヒド, 臭気, 悪臭, 臭い, 臭気成分, ニオイ, 有機溶剤蒸気
    P_particulate: 粒子状物質 | PM2.5, パティキュレート, 微粒子状物質
    P_radioactive: 放射性セシウム | 放射性物質, セシウム, 放射性ストロンチウム, 放射性ヨウ素, 放射性核種, 放射性元素, 放射能, ウラン, アクチノイド, 放射性
    P_heavy_metal: 重金属 | ヒ素, 砒素, 水銀, 鉛イオン, 六価クロム, カドミウム, 重金属イオン, フッ素イオン
    P_organic_pollutant: 有機フッ素化合物 | PFAS, PFOS, PFOA, トリハロメタン, ダイオキシン, 有機塩素化合物, 残留塩素, 遊離塩素, 有機塩化物
    P_carbon_monoxide: 一酸化炭素 | CO中毒
    P_hazard_general: 有害物質 | 汚染物質, 有害成分, 有害ガス, 有害な, 汚染, 不要成分, 有害
  P_other: その他の課題
    P_novelty: 新規な | 新規の, 新たな, 新しい, 新規, 代替材料, 代替となる
M: 解決手段
  M_porous_material: 多孔質骨格材料
    M_zeolite_general: ゼオライト | ゼオライト系, 合成ゼオライト, 分子篩, 分子ふるい, モレキュラーシーブ, モレキュラシーブ, アルミノシリケート, アルミノケイ酸塩, 結晶性アルミノシリケート, 小細孔ゼオライト, 8員環
    M_zeolite_cha: CHA型 | チャバザイト, SSZ-13, SAPO-34, CHA型ゼオライト
    M_zeolite_aei: AEI型 | SSZ-39, AEI型ゼオライト
    M_zeolite_mfi: MFI型 | ZSM-5, シリカライト, MFI型ゼオライト
    M_zeolite_beta: ベータ型 | ベータ型ゼオライト, β型ゼオライト, BEA型, *BEA
    M_zeolite_fau: FAU型 | フォージャサイト, ホージャサイト, Y型ゼオライト, X型ゼオライト, USY, 13X, ゼオライトY, ゼオライトX
    M_zeolite_mor: モルデナイト | MOR型, モルデナイト型
    M_zeolite_lta: LTA型 | A型ゼオライト, ゼオライトA, LTA型ゼオライト
    M_zeolite_other: FER型 | フェリエライト, DDR型, STT型, AFX型, ERI型, LEV型, MWW型, MCM-22, RHO型, MTW型, TON型, AFI型, CON型, GIS型
    M_natural_zeolite: 天然ゼオライト | クリノプチロライト, モルデナイト系天然
    M_aluminophosphate: アルミノリン酸塩 | SAPO, AlPO, シリコアルミノリン酸塩, アルミノフォスフェート, シリコアルミノフォスフェート, リン酸アルミニウム系
    M_titanosilicate: チタノシリケート | TS-1, ETS-10, チタノケイ酸, チタノケイ酸塩, 結晶性チタノシリケート
    M_coordination_framework: 金属有機構造体 | 多孔性金属有機構造体, MOF, ZIF-8, metal organic framework, metal−organic framework
  M_silica_clay: シリカ・ケイ酸塩・粘土
    M_mesoporous_silica: メソポーラスシリカ | MCM-41, SBA-15, メソ多孔体, メソ多孔質シリカ, 規則性メソ多孔
    M_silica_gel: シリカゲル | 球状シリカゲル, 多孔質シリカ, 多孔性シリカ, シリカ多孔体, 非晶質シリカ, 沈降シリカ, 沈殿シリカ, ヒュームドシリカ, シリカ粒子, シリカ
    M_aerogel: エアロゲル | シリカエアロゲル, エアロジェル, キセロゲル
    M_hollow_silica: 中空シリカ | 中空シリカ粒子, 中空粒子, 中空構造
    M_colloidal_silica: コロイダルシリカ | シリカゾル, 水ガラス, ケイ酸ナトリウム, ケイ酸ソーダ, 珪酸ソーダ
    M_clay: 粘土鉱物 | モンモリロナイト, ベントナイト, セピオライト, カオリン, スメクタイト, ハイドロタルサイト, 層状複水酸化物, 層状ケイ酸塩, 珪藻土, ケイ藻土, 活性白土, パリゴルスカイト, イモゴライト, アロフェン, 粘土
    M_glass: 多孔質ガラス | ガラス, ガラス繊維, ガラスフリット, 結晶化ガラス
  M_carbon_material: 炭素材料
    M_activated_carbon: 活性炭 | 粉末活性炭, 粒状活性炭, 破砕炭, 成形活性炭, 球状活性炭, 繊維状活性炭, 賦活炭
    M_activated_carbon_fiber: 活性炭素繊維 | 活性炭繊維, 炭素繊維, ACF
    M_carbon_black: カーボンブラック | アセチレンブラック, ケッチェンブラック, ファーネスブラック, 導電性カーボン
    M_graphite: 黒鉛 | グラファイト, 人造黒鉛, 天然黒鉛, 膨張黒鉛, 黒鉛化, 黒鉛質, 球状黒鉛
    M_graphene: グラフェン | 酸化グラフェン, 還元型酸化グラフェン, グラフェンシート, ナノグラフェン
    M_cnt: カーボンナノチューブ | CNT, 単層カーボンナノチューブ, 多層カーボンナノチューブ, ナノチューブ, カーボンナノファイバー
    M_hard_carbon: ハードカーボン | 難黒鉛化性炭素, 難黒鉛化炭素, ソフトカーボン, 易黒鉛化性炭素, 非晶質炭素, アモルファスカーボン
    M_porous_carbon: 多孔質炭素 | 多孔性炭素, 多孔質炭素材料, メソポーラスカーボン, メソ孔炭素, 炭素多孔体, 多孔質カーボン, 分子ふるい炭素, モレキュラーシーブカーボン, 炭素質材料, 炭素材料, 炭素質, カーボン材料
    M_char: 炭化物 | 木炭, 竹炭, バイオ炭, 炭化物粒子, チャー, 炭素化物
  M_carbon_precursor: 炭素原料
    M_biomass_precursor: ヤシ殻 | 椰子殻, やし殻, ココナッツ, 木質, 木材, おが屑, おがくず, もみ殻, 籾殻, バイオマス, 植物由来, 果実殻, コーヒー, セルロース, リグニン, 木粉, 竹材
    M_fossil_precursor: 石炭 | 石油ピッチ, ピッチ, コールタール, コークス, 石油コークス, 褐炭, 亜炭, 瀝青炭, 重質油, メソフェーズ
    M_resin_precursor: フェノール樹脂 | 合成樹脂, 樹脂原料, ポリアクリロニトリル, PAN系, ポリ塩化ビニリデン, ポリイミド, 熱硬化性樹脂, 糖類, スクロース, 有機高分子
  M_oxide_material: 金属酸化物
    M_alumina: アルミナ | 酸化アルミニウム, 活性アルミナ, γ-アルミナ, α-アルミナ, ベーマイト, 擬ベーマイト, 水酸化アルミニウム, θ-アルミナ
    M_zirconia: ジルコニア | 酸化ジルコニウム, 安定化ジルコニア, イットリア安定化ジルコニア, YSZ, セリア-ジルコニア, セリアジルコニア
    M_titania: 酸化チタン | チタニア, 二酸化チタン, アナターゼ, アナタース, ルチル, チタン酸化物, 酸化チタン粒子
    M_ceria: セリア | 酸化セリウム, セリウム酸化物, 酸素吸蔵材, 酸素貯蔵材, 酸素吸放出
    M_other_oxide: 酸化亜鉛 | 酸化マグネシウム, マグネシア, 酸化鉄, 酸化マンガン, 二酸化マンガン, 酸化タングステン, 酸化ニッケル, 酸化銅, 酸化スズ, 酸化イットリウム, 酸化ランタン, 酸化コバルト, 酸化モリブデン, 酸化バナジウム, 五酸化バナジウム, 酸化ニオブ, 酸化ガリウム, 酸化インジウム, 酸化カルシウム
    M_mixed_oxide: 複合酸化物 | ペロブスカイト, ペロブスカイト型, スピネル, スピネル型, 複合金属酸化物, 混合酸化物, 固溶体, パイロクロア, ガーネット, シリカアルミナ, シリカ-アルミナ, アルミノシリカ
    M_salt_compound: リン酸カルシウム | ハイドロキシアパタイト, アパタイト, 炭酸カルシウム, リン酸塩, 炭酸塩, 硫酸塩, 水酸化物, 層状化合物, ケイ酸カルシウム, ケイ酸塩, 硫化物, 窒化物, ハロゲン化物
  M_ceramic: セラミックス材料
    M_nonoxide_ceramic: 炭化ケイ素 | 炭化珪素, SiC, 窒化ケイ素, 窒化珪素, 窒化アルミニウム, 窒化ホウ素, 六方晶窒化ホウ素, 炭化ホウ素, 炭化チタン, 窒化チタン, サイアロン, 炭化タングステン
    M_oxide_ceramic: コーディエライト | ムライト, チタン酸アルミニウム, チタン酸バリウム, ステアタイト, フォルステライト, 酸化物セラミックス
    M_sintered_body: 焼結体 | セラミックス焼結体, セラミック焼結体, 緻密体, 多結晶体, セラミックス, セラミック, 焼成体
    M_honeycomb: ハニカム構造体 | ハニカム, ハニカムフィルタ, ハニカム基材, ハニカム成形体, 目封止, セル隔壁, 隔壁, ウォールフロー, モノリス, ハニカム状
    M_porous_body: 多孔質体 | 多孔体, 多孔質セラミックス, 多孔質セラミック, 発泡体, 多孔質構造体, 多孔質基材, 多孔質焼結体, 多孔質材, 多孔質材料, 多孔性材料, 多孔質粒子, 多孔性
  M_membrane: 分離膜
    M_zeolite_membrane: ゼオライト膜 | ゼオライト分離膜, ゼオライト膜複合体, ゼオライト膜構造体, 膜複合体
    M_membrane_general: 分離膜 | ガス分離膜, 無機膜, 無機分離膜, 選択透過膜, シリカ膜, 炭素膜, セラミック膜, セラミックス膜, 中空糸膜, 中空糸, 管状膜, 緻密膜, 分離層, 透過膜, 精密ろ過膜, 限外ろ過膜, 逆浸透膜, 水素分離膜, 膜モジュール
    M_support: 多孔質支持体 | 支持体, 基材, 多孔質基体, 多孔質支持層, 支持層, 基体
  M_metal_component: 金属成分
    M_noble_metal: 貴金属 | 白金, パラジウム, ロジウム, ルテニウム, イリジウム, 白金族, 白金族元素, 白金族金属, 金ナノ粒子, 銀ナノ粒子, 金属銀
    M_transition_metal: 遷移金属 | ニッケル, コバルト, マンガン, モリブデン, タングステン, バナジウム, クロム, 亜鉛, ニオブ, ジルコニウム, チタン, 鉄化合物, 銅化合物, 銅イオン, 鉄イオン, 銀イオン, 卑金属, 遷移金属元素
    M_rare_earth: 希土類 | 希土類元素, レアアース, ランタン, イットリウム, ネオジム, プラセオジム, ランタノイド, セリウム
    M_alkaline_metal: アルカリ金属 | アルカリ土類金属, ナトリウム, カリウム, マグネシウム, カルシウム, バリウム, ストロンチウム, アルカリ金属元素, アルカリ土類
    M_main_group: アルミニウム | ケイ素, 珪素, ホウ素化合物, ガリウム, ゲルマニウム, スズ, ビスマス, インジウム, リン化合物, リン元素, ホウ素
    M_metal_particle: 金属微粒子 | 金属粒子, 金属ナノ粒子, ナノ粒子, 合金, 合金粒子, 金属クラスター, 担持金属, 活性金属, 触媒金属, 触媒成分, 活性成分
  M_battery_material: 電池材料
    M_cathode_general: 正極活物質 | 正極材料, 正極材, リチウム遷移金属複合酸化物, リチウム含有複合酸化物, リチウム複合酸化物, リチウム遷移金属酸化物, 正極活物質粒子, リチウム金属複合酸化物
    M_active_material_general: 活物質 | 電極活物質, 活物質粒子, 電極活性物質, 活性物質
    M_layered_oxide: 層状岩塩型 | ニッケルコバルトマンガン, ニッケルコバルトアルミニウム, NCM, NCA, ハイニッケル, LiNiO2, ニッケル酸リチウム, コバルト酸リチウム, LiCoO2, リチウム過剰, LiNiMnCoO2, リチウムニッケル複合酸化物, Li2MnO3, 層状構造
    M_spinel_cathode: マンガン酸リチウム | LiMn2O4, スピネル型リチウムマンガン, リチウムマンガン複合酸化物, ニッケルマンガン酸リチウム
    M_olivine: オリビン型 | リン酸鉄リチウム, LiFePO4, LiMPO4, LiCoPO4, リン酸マンガンリチウム, ポリアニオン
    M_anode_general: 負極活物質 | 負極材料, 負極材, 負極活物質粒子
    M_silicon_anode: シリコン系 | ケイ素系, 珪素系, 酸化ケイ素, 一酸化ケイ素, SiOx, シリコン粒子, シリコン, Si系, ケイ素含有
    M_titanate_anode: チタン酸リチウム | Li4Ti5O12, LTO, チタン酸化合物, チタン酸
    M_lithium_metal: 金属リチウム | リチウム金属, リチウム箔, リチウム合金
    M_sulfide_electrolyte: 硫化物固体電解質 | 硫化物系固体電解質, 硫化物系, アルジロダイト, Li2S-P2S5, LGPS, チオリシコン, 硫化リチウム, 五硫化二リン, P2S5
    M_oxide_electrolyte: 酸化物固体電解質 | ガーネット型, LLZ, LLZO, NASICON, ナシコン型, LATP, LAGP, リン酸チタンアルミニウムリチウム, 酸化物系固体電解質
    M_solid_electrolyte_general: 固体電解質 | 無機固体電解質, 固体電解質材料, 固体電解質層, ハロゲン化物固体電解質, 電解質層
    M_liquid_electrolyte: 電解液 | 非水電解液, 非水電解質, 電解質塩, リチウム塩, LiPF6, LiBF4, イオン液体, 有機電解液, 電解質溶液, 非水溶媒, 電解質, プロピレンカーボネート, エチレンカーボネート, ジメチルカーボネート, ジエチルカーボネート, エチルメチルカーボネート, 炭酸エチレン, 炭酸プロピレン
    M_cell_component: 集電体 | セパレータ, セパレーター, 導電助剤, 導電材, 電極合材, 合材層, 活物質層, 電極層, 正極層, 負極層, 電極材料, 電極シート
  M_organic_component: 有機成分
    M_template: 構造規定剤 | 有機構造規定剤, OSDA, SDA, テンプレート, 鋳型, 界面活性剤, 第四級アンモニウム, 4級アンモニウム, テトラプロピルアンモニウム, テトラエチルアンモニウム, アダマンタン, 有機テンプレート, 鋳型剤
    M_amine: アミン | アミン化合物, ポリエチレンイミン, アミノ基, 有機アミン, アルカノールアミン, ポリアミン
    M_polymer: 高分子 | 樹脂, ポリマー, バインダー樹脂, 熱可塑性樹脂, ポリビニル, ポリオレフィン, フッ素樹脂, ポリフッ化ビニリデン, PVDF, ポリテトラフルオロエチレン, PTFE, 有機ポリマー, ポリエチレン, ポリプロピレン, ポリスチレン, ニトリルゴム, ポリアクリル酸, ポリエチレングリコール, ポリビニルアルコール, カルボキシメチルセルロース
    M_coupling_agent: シランカップリング剤 | カップリング剤, シラン化合物, オルガノシラン, 有機シラン, シロキサン, アルコキシシラン, テトラエトキシシラン, TEOS, テトラメトキシシラン
  M_structure_spec: 構造・組成の規定
    M_pore_structure: 細孔径 | 細孔分布, 細孔径分布, 細孔容積, 細孔容量, 細孔構造, ミクロ孔, メソ孔, マクロ孔, 平均細孔径, 階層的細孔, 階層構造, 気孔率, 空隙率, 多孔度, 細孔直径, 孔径, 細孔, 気孔, 空孔, 空隙, マイクロ孔, 細孔分布曲線, 全細孔容積, 水銀圧入, 水銀ポロシメータ, 水銀ポロシメトリ, 水銀圧入法
    M_surface_area: 比表面積 | BET比表面積, 表面積, 外表面積, BET表面積, 比表面積値
    M_particle_morphology: 粒子径 | 平均粒子径, 粒径, 平均粒径, 粒度分布, 一次粒子, 二次粒子, メジアン径, D50, 球状粒子, 微粒子, 凝集体, アスペクト比, 繊維状, 板状, 鱗片状, コアシェル, 粒子形状, 粒度, ナノシート, ナノワイヤ, ナノロッド
    M_crystal_structure: 結晶構造 | 結晶性, 結晶子径, 結晶子サイズ, X線回折, XRD, 回折ピーク, 非晶質, アモルファス, 単結晶, 結晶相, 骨格構造, 結晶格子, 格子定数, 結晶面, 回折角, ピーク強度, 積分強度, 回折強度
    M_composition_ratio: モル比 | 組成比, 含有量, 元素比, SiO2/Al2O3, シリカ/アルミナ比, Si/Al比, 原子比, 質量比, 重量比, 含有率, 組成式, 一般式, 化学組成, 配合比, 添加量, 質量%, 重量%, 原子%, 容量%, 体積%
    M_surface_property: 酸点 | 酸量, 酸強度, 固体酸, 疎水性, 親水性, 表面官能基, 官能基, 酸性官能基, 含酸素官能基, 表面電荷, ゼータ電位, 水酸基, シラノール, 塩基性, 酸性度, 疎水化, 親水化, 表面酸素, 撥水性
    M_density: 嵩密度 | かさ密度, 充填密度, 真密度, タップ密度, 見掛け密度, 密度, 圧縮密度, 電極密度, 嵩比重
  M_synthesis: 合成方法
    M_hydrothermal: 水熱合成 | 水熱処理, 水熱反応, ソルボサーマル, オートクレーブ, 水熱条件, 熱水合成
    M_solgel: ゾルゲル法 | ゾル-ゲル, ゾルゲル, 加水分解, 縮重合, アルコキシド, 金属アルコキシド, ゲル化, 湿潤ゲル
    M_precipitation: 共沈法 | 共沈, 沈殿法, 沈殿, 中和, 晶析, 液相法, 湿式法
    M_solid_state: 固相反応 | 固相法, 固相合成, 乾式法, 溶融法, 溶融塩, フラックス, 自己燃焼
    M_vapor_phase: 化学気相成長 | 化学気相蒸着, CVD, 気相成長, 蒸着, スパッタ, スパッタリング, 気相法, 原子層堆積, プラズマ処理
    M_seed: 種結晶 | 種晶, シード, 種子結晶, 結晶化, 結晶成長
    M_spray: 噴霧乾燥 | スプレードライ, 噴霧熱分解, スプレードライヤー
    M_milling: 粉砕 | ボールミル, ミリング, メカノケミカル, メカニカルミリング, ジェットミル, 解砕, 微粉砕, 破砕, 分級, 混練
    M_mixing: 混合 | 撹拌, 攪拌, 分散液, スラリー, 混合溶液, 原料混合物, 前駆体, 前駆体溶液, 原料組成物, 反応混合物
    M_solvent: 溶媒 | 有機溶媒, 溶剤, 有機溶剤, 水系溶媒, 分散媒, エタノール, イソプロパノール, エチレングリコール, N-メチルピロリドン, NMP, アセトン, アセトニトリル, トルエン, 水溶媒, プロピレングリコール
  M_thermal_treatment: 熱処理
    M_calcination: 焼成 | 仮焼, 本焼成, 熱処理, 加熱処理, アニール, 焼成工程, 焼成温度, 熱処理工程
    M_sintering: 焼結 | ホットプレス, 放電プラズマ焼結, 常圧焼結, 焼き固め, 焼結助剤, 反応焼結
    M_carbonization: 炭化 | 炭素化, 熱分解, 炭化処理, 炭化工程, 乾留, 不融化, 耐炎化
    M_drying: 乾燥 | 凍結乾燥, 超臨界乾燥, 減圧乾燥, 真空乾燥, 乾燥工程, 常圧乾燥
  M_activation: 賦活
    M_activation_general: 賦活処理 | 賦活工程, 賦活化, 賦活剤, 賦活
    M_gas_activation: 水蒸気賦活 | ガス賦活, 二酸化炭素賦活, 炭酸ガス賦活, 物理賦活, 空気賦活, 水蒸気を含むガス
    M_chemical_activation: 薬品賦活 | 薬剤賦活, アルカリ賦活, 化学賦活, 水酸化カリウム, 塩化亜鉛, リン酸賦活, 水酸化ナトリウム, アルカリ金属水酸化物, KOH
  M_post_treatment: 後処理・改質
    M_ion_exchange: イオン交換 | カチオン交換, イオン交換処理, プロトン型, アンモニウム型, 金属交換, イオン交換容量, 交換容量, イオン交換体, 交換性陽イオン
    M_acid_treatment: 酸処理 | 酸洗浄, 脱アルミニウム, 脱アルミ, 塩酸処理, 硝酸処理, 硫酸処理, 酸洗, 酸溶液
    M_surface_modification: 表面処理 | 表面修飾, 表面改質, 疎水化処理, 官能基化, 化学修飾, グラフト, 表面処理剤
    M_redox_treatment: 還元処理 | 酸化処理, 水素還元, 還元雰囲気, 酸化雰囲気, 還元工程, 還元剤, 酸化剤, 不活性雰囲気, 不活性ガス雰囲気
    M_washing: 洗浄 | 水洗, 洗浄工程, 精製工程, 脱塩, 濾過, ろ過
    M_steaming: スチーミング | 水蒸気処理, 水蒸気雰囲気, 水熱エージング
  M_loading: 担持・複合化
    M_supported: 担持 | 担持体, 担持量, 含浸, 含浸法, 含浸担持, 担体, 触媒担体, 坦持, 担持触媒, 固定化
    M_coating: 被覆 | コーティング, 被覆層, 表面被覆, コート層, 炭素被覆, カーボンコート, 被膜, 皮膜, 保護層, 被覆材
    M_doping: ドープ | ドーピング, 置換元素, 元素置換, 添加元素, 固溶, ドーパント, 異種元素
    M_composite: 複合化 | 複合材料, 複合体, ハイブリッド, ナノコンポジット, 複合粒子, コンポジット, 複合材
  M_forming: 成形・膜形成
    M_shaping: 成形 | 成型, 押出成形, 押出し成形, 造粒, 打錠, ペレット, 成形体, 顆粒, 造粒物, 成形物, 球状成形, ビーズ
    M_binder: バインダー | バインダ, 結着剤, 結合剤, 無機バインダ, 無機バインダー, 有機バインダ, 接着剤, スチレンブタジエンゴム, SBR
    M_film_formation: 製膜 | 成膜, 塗布, 塗工, ディップコート, スラリー塗布, 積層, 薄膜, 膜形成, 積層体
  M_operation: 運転・プロセス条件
    M_swing_adsorption: 圧力スイング吸着 | 圧力スイング, PSA, 温度スイング, TSA, 圧力変動吸着, 減圧再生, 加熱再生, 再生工程, 脱着工程, 吸着工程
    M_reactor: 固定床 | 流動床, 反応器, 反応塔, 吸着塔, 充填塔, カラム, 充填層, 流通式, 反応管, 吸着槽, 吸着装置
    M_reaction_condition: 反応温度 | 反応圧力, 空間速度, 接触時間, 反応条件, 処理温度, 運転温度, 接触させる, 接触させ
    M_membrane_process: 膜分離 | 膜分離法, 浸透気化, パーベーパレーション, 蒸気透過, 透過側, 供給側
U: 用途
  U_environment: 環境浄化
    U_vehicle_exhaust: 排ガス浄化 | 排気ガス浄化, 排ガス処理, 排ガス浄化用, 三元触媒, 自動車排ガス, 自動車用排ガス, 内燃機関, ディーゼルエンジン, ガソリンエンジン, ディーゼル, DPF, パティキュレートフィルタ, 尿素SCR, SCR触媒, 選択還元型, 選択的還元, 排気浄化, 排ガス, 排気ガス, エンジン, 電気加熱式, EHC
    U_industrial_exhaust: 燃焼排ガス | 工場排ガス, 煙道ガス, 排煙, 脱硝, 排煙脱硫, ボイラ, 焼却炉, 焼却, 発電所, 火力発電, ガス処理, オフガス
    U_water_treatment: 水処理 | 浄水, 排水処理, 廃水処理, 浄水器, 水道水, 上水, 下水, 飲料水, 地下水, 海水淡水化, 水質浄化, 浄水処理, 廃水, 汚水, 純水製造, 超純水, 油水分離処理
    U_air_purification: 脱臭 | 消臭, 空気清浄, 空気清浄機, 室内空気, 防臭, 消臭剤, 脱臭剤, 換気, 空気浄化, 脱臭フィルタ
    U_decontamination: 除染 | 汚染水, 原子力, 原子力発電所, 原発, 放射性廃棄物, 汚染土壌, 土壌浄化, 土壌汚染
    U_agriculture: 土壌改良 | 土壌, 肥料, 農業, 園芸, 堆肥, 家畜, 飼料, 植物栽培, 農薬, 防除剤, 有害生物
    U_recycling: リサイクル | 再資源化, 廃プラスチック, 廃棄物処理, 再生利用, 有価金属, 金属回収, 貴金属回収, 回収金属, 湿式製錬, 精錬, 製錬
  U_gas_handling: ガスの分離・回収・貯蔵
    U_co2_capture: 二酸化炭素回収 | CO2回収, 二酸化炭素の回収, 二酸化炭素を回収, 二酸化炭素分離, 二酸化炭素の分離, CO2分離, CO2の分離, 二酸化炭素吸着, CO2吸着, CCS, CCUS, 直接空気回収, 二酸化炭素除去, 二酸化炭素の除去, 炭素回収, 二酸化炭素の固定
    U_gas_separation: ガス分離 | 気体分離, 空気分離, 酸素濃縮, 窒素製造, 水素精製, 水素分離, 天然ガス, バイオガス, メタン分離, オレフィン分離, 炭化水素分離, 希ガス, ガス精製, 混合ガス, 混合気体, プロピレン分離, 酸素製造, 酸素分離, 酸素を分離, 高純度ガス, ガスの精製
    U_gas_storage: 水素貯蔵 | ガス貯蔵, 吸蔵, メタン貯蔵, 天然ガス貯蔵, 吸着貯蔵, 水素吸蔵
    U_dehumidification: 除湿 | 乾燥剤, 調湿, 吸湿剤, デシカント, 除湿剤, 湿度調整
    U_liquid_separation: 脱水分離 | アルコール脱水, 溶剤回収, 液体分離, 脱水膜, 水とアルコール, アルコールの脱水, エタノールの脱水, 油水分離
    U_canister: キャニスタ | キャニスター, 蒸散燃料, 燃料蒸発ガス, ガソリン蒸気
  U_chemical_process: 化学プロセス
    U_petrochemical: 石油精製 | 水素化分解, 水素化精製, 脱硫, 接触分解, クラッキング, 異性化, 芳香族化, 軽質オレフィン, オレフィン製造, プロピレン製造, ブタジエン, 石油化学, ナフサ, 重質油の分解, 水素化処理, エチレン製造, エチレンの製造, プロピレンの製造, 炭化水素
    U_fine_chemical: 化学品 | ファインケミカル, 有機合成, 医薬中間体, 酸化反応, 水素化反応, エポキシ化, 不飽和アルデヒド, 不飽和カルボン酸, アクリル酸, メタクリル酸, 化学品製造, 化成品, 化学工業, 合成反応, アミノ化, アミン合成, 重水素, 有機化合物の製造, グリコール製造, ニトリル製造
    U_c1_chemistry: メタノール合成 | メタン化, メタネーション, フィッシャー・トロプシュ, FT合成, 合成ガス, 水性ガスシフト, シフト反応, 二酸化炭素の水素化, CO2の水素化, ドライリフォーミング, 水蒸気改質, 改質反応, 改質器, ジメチルエーテル, 燃料合成, 合成燃料, メタノール製造, CO2還元, 二酸化炭素の還元, CO2の還元, 二酸化炭素還元
    U_hydrogen_energy: 水素製造 | 水素生成, 水分解, 光触媒による水分解, アンモニア合成, アンモニア分解, 人工光合成, 水素発生, 水電解, 水素ステーション
    U_photocatalysis: 抗菌 | 抗ウイルス, 防汚, セルフクリーニング, 殺菌, 防カビ, 抗菌剤, 光触媒用途
  U_energy_device: エネルギーデバイス
    U_lib: リチウムイオン二次電池 | リチウムイオン電池, リチウム二次電池, 非水電解質二次電池, 非水電解液二次電池, リチウムイオンバッテリー, Liイオン電池, Liイオン二次電池, リチウムイオンバッテリ, リチウムイオンセル
    U_battery_general: 電池 | 二次電池, 蓄電池, 電池用, バッテリー, バッテリ, 蓄電デバイス, 蓄電素子, 電気化学デバイス, 電気化学素子, 電気化学セル, 電池パック, 電池セル, 電池モジュール, 単電池, 蓄電装置
    U_solid_state_battery: 全固体電池 | 全固体二次電池, 全固体リチウムイオン, 全固体リチウム, 固体電池, 全固体型
    U_post_li: ナトリウムイオン電池 | ナトリウムイオン二次電池, リチウム硫黄電池, リチウム空気電池, 空気電池, 金属空気電池, マグネシウム電池, カリウムイオン電池, 多価イオン電池, ナトリウム二次電池, 亜鉛空気電池, レドックスフロー, フローバッテリー, フロー電池
    U_capacitor: 電気二重層キャパシタ | キャパシタ, キャパシター, スーパーキャパシタ, リチウムイオンキャパシタ, EDLC, 電気二重層コンデンサ, コンデンサ, ハイブリッドキャパシタ
    U_fuel_cell: 燃料電池 | 固体高分子形燃料電池, 固体酸化物形燃料電池, SOFC, PEFC, 固体高分子型燃料電池, 燃料電池用, 空気極, 電極触媒, 酸素還元
    U_solar_thermal: 太陽電池 | 光電変換, 太陽光発電, 熱電変換, ヒートポンプ, 吸着式冷凍, 蓄熱材, 冷凍機, 空調, 吸着式ヒートポンプ, 冷熱, 相変化材料, 潜熱蓄熱, 熱制御, 熱管理
  U_application_platform: 機器・移動体
    U_vehicle: 電気自動車 | ハイブリッド自動車, プラグインハイブリッド, 車載, 車載用, 自動車, 車両, 電動車両, 移動体, 自動車用, 鉄道, 船舶, 航空機
    U_portable: 携帯電話 | ノートパソコン, 携帯機器, 電子機器, スマートフォン, モバイル, 携帯用, 電動工具, ウェアラブル, 家電, 情報端末, ドローン
    U_stationary: 定置用 | 電力貯蔵, 蓄電システム, 系統連系, 再生可能エネルギー, 風力発電, 非常用電源, バックアップ電源, 電力系統, 電源, エネルギー貯蔵
  U_products: 製品・産業用途
    U_medical: 医薬品 | 医薬, 医療, ドラッグデリバリー, 薬物送達, 生体材料, 人工骨, 透析, 血液浄化, 経口吸着剤, 医療用, 歯科, 生体, 薬剤, 骨補填材, 治療, 疾患, 予防, 治療用
    U_consumer: 化粧品 | 化粧料, 衛生用品, 日用品, 紙おむつ, 繊維製品, 衣料, 寝具, ペット, タバコ, たばこ, 生活用品, 歯磨, スクラブ剤, 洗浄剤, 徐放, 芳香剤
    U_food: 食品 | 飲料, 食品添加物, 脱色, 糖液, 酒類, 食用油, 食品工業, 食品用, 調味料, 精糖
    U_filler: フィラー | 充填剤, 樹脂組成物, 塗料, インク, 顔料, ゴム, 補強材, 艶消し, 樹脂用, 化粧材, 塗膜
    U_electronics: 半導体 | 電子部品, 半導体製造, 回路基板, 絶縁基板, 放熱基板, 放熱部材, センサ, ガスセンサ, ディスプレイ, 研磨剤, 研磨材, 誘電体, 発光素子, 光学部材, 光学材料, 半導体装置, パワーモジュール, ウェハ, ウエハ, 電波吸収, ステルス, スピーカー, 検知, センサー
    U_analysis: クロマトグラフィー | 液体クロマトグラフィー, ガスクロマトグラフィー, カラム充填剤, 固相抽出, 試料前処理, 分析用, 分析装置
    U_construction: 建材 | 建築材料, 建築物, コンクリート, セメント, 構造材, 断熱材, 断熱, 耐火物, 耐火材, 保温材, 壁材, 炉材, 住宅
    U_machinery: 切削工具 | 軸受, ベアリング, 摺動部材, 耐摩耗部材, 機械部品, 工具, 治具, 耐熱部材, 構造部材, 半導体製造装置用, クライオポンプ, 真空装置, 真空ポンプ
    U_sorbent_use: 吸着剤 | 吸着材, 吸着材料, 吸着用, 吸着体, 吸着フィルタ, フィルタ, フィルター, 濾材, ろ材, 浄化材, 吸収剤, 吸収材, 化学吸収剤, 捕集剤, 捕集材, 浄化剤
    U_catalyst_use: 触媒用途 | 触媒材料, 担体材料, 触媒担体用, 工業触媒, 固体触媒, 触媒反応, 触媒プロセス, 触媒として, 触媒用途, 用触媒, 触媒に用い
"""

# 述語。group が同じなら近い（0.7）、polarity が逆なら 0。
PREDICATES = [
    ("addresses", "手段が課題を解決する", "reduce", -1),
    ("used_for", "手段が用途に使われる", "apply", 0),
    ("arises_in", "課題が用途で問題になる", "locate", 0),
    ("suppresses", "抑制する（互換）", "reduce", -1),
    ("prevents", "防止する（互換）", "reduce", -1),
    ("causes", "引き起こす（互換）", "induce", 1),
    ("degrades", "劣化させる（互換）", "induce", 1),
    ("applied_to", "適用先（互換）", "locate", 0),
    ("occurs_in", "生じる場所（互換）", "locate", 0),
]
TOP_LABELS = {"P": "課題", "M": "解決手段", "U": "用途"}
DASHES = ["-", "−", "‐"]
# MOF に特有の語は、無機材料 1,200 件の本文に実際に出た表記だけを入れる（ハイフンの異表記を自動で足さない）。
# 出た文書（課題・解決手段・用途の段落）: 金属有機構造体 I0229 I0277 I0436 I0949 / 多孔性金属有機構造体 I0277 /
# MOF I0277 I0786 I0885 I0949 / ZIF-8 I0229 / metal organic framework I0885 / metal−organic framework I0786
NO_EXPAND = {"M_coordination_framework"}


# ---------------------------------------------------------------------------
# 木の読み込み
# ---------------------------------------------------------------------------
def parse_tree(text: str = TREE) -> list[dict]:
    """TREE を概念の一覧（上位型を除く）にする。"""
    out: list[dict] = []
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        depth = (len(raw) - len(raw.lstrip(" "))) // 2
        line = raw.strip()
        head, _, alias_part = line.partition("|")
        cid, _, label = head.partition(":")
        cid, label = cid.strip(), label.strip()
        aliases = [a.strip() for a in alias_part.split(",") if a.strip()] if alias_part else []
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if depth == 0:
            if cid not in TOP_LABELS:
                raise ValueError(f"上位型は P/M/U のみ: {cid}")
            stack.append((0, cid))
            continue
        parent = stack[-1][1]
        out.append({"id": cid, "label": label, "aliases": aliases, "parents": [parent]})
        stack.append((depth, cid))
    return out


def expand_aliases(label: str, aliases: list[str], expand: bool = True) -> list[str]:
    """ハイフンの異表記を足し、重複・表示名と同じもの・短すぎる英字を除く。"""
    seen = {unicodedata.normalize("NFKC", label).lower()}
    out: list[str] = []

    def add(s: str) -> None:
        n = unicodedata.normalize("NFKC", s).lower()
        if n in seen:
            return
        if re.fullmatch(r"[A-Za-z0-9]{1,2}", n):
            raise ValueError(f"1〜2 文字の英数字の別名は入れない: {s}")
        seen.add(n)
        out.append(s)

    for a in aliases:
        if expand and any(d in a for d in DASHES):
            base = a
            for d in DASHES:
                base = base.replace(d, "-")
            for d in DASHES:
                add(base.replace("-", d))
            add(base.replace("-", ""))
        else:
            add(a)
    if expand and any(d in label for d in DASHES):
        base = label
        for d in DASHES:
            base = base.replace(d, "-")
        for d in DASHES:
            add(base.replace("-", d))
        add(base.replace("-", ""))
    return out


def concepts_data() -> list[dict]:
    """YAML に書く概念の一覧（別名は展開済み）。"""
    cs = parse_tree()
    ids = [c["id"] for c in cs]
    dup = [x for x, n in collections.Counter(ids).items() if n > 1]
    if dup:
        raise ValueError(f"ID の重複: {dup}")
    for c in cs:
        if not c["id"].startswith(c["parents"][0][0]):
            raise ValueError(f"ID の頭文字が上位型と合わない: {c['id']}")
        c["aliases"] = expand_aliases(c["label"], c["aliases"], expand=c["id"] not in NO_EXPAND)
    # 同じ表記が 2 つの概念に入っていないか
    owner: dict[str, str] = {}
    clashes: list[str] = []
    for c in cs:
        is_leaf = not any(c["id"] in d["parents"] for d in cs)
        forms = ([c["label"]] if is_leaf or c["aliases"] else []) + c["aliases"]
        for s in forms:
            n = unicodedata.normalize("NFKC", s).lower()
            if n in owner and owner[n] != c["id"]:
                clashes.append(f"{s}: {owner[n]} / {c['id']}")
            owner[n] = c["id"]
    if clashes:
        raise ValueError("同じ表記が 2 つの概念にある: " + "; ".join(clashes))
    return cs


# ---------------------------------------------------------------------------
# YAML の書き出し
# ---------------------------------------------------------------------------
def q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def tree_hash(cs: list[dict]) -> str:
    blob = json.dumps(cs, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def build_yaml() -> Path:
    """config/ontology_inorganic_v1.yaml を書く。"""
    cs = concepts_data()
    parents = {p for c in cs for p in c["parents"]}
    n_leaf = sum(1 for c in cs if c["id"] not in parents)
    n_alias = sum(len(c["aliases"]) for c in cs)
    lines = [
        f"# オントロジー {VERSION}（無機材料の国内特許 1,200 件から作成）",
        f"# 版の固定日: {FROZEN_ON}。この版は固定。変更するときは version を上げて別ファイルにする。",
        f"# 内容のハッシュ（概念・別名・親の sha256 先頭 16 桁）: {tree_hash(cs)}",
        "# 作り方: scripts/build_ontology_inorganic.py（datasets/inorganic1200/documents_sections.csv の",
        "#   doc_id 奇数＝開発用だけで候補語を数え、人が階層と別名を決めた）。報告: datasets/inorganic1200/ontology_report.md",
        "# MOF（金属有機構造体）／PCP の評価用データは見ずに作った。",
        "# 上位型は 課題(P)・解決手段(M)・用途(U)。上位型だけを共有する概念どうしの類似度は 0。",
        f"# 概念 {len(cs)}（中間 {len(cs) - n_leaf}・末端 {n_leaf}）、別名 {n_alias}。",
        f'version: "{VERSION}"',
        "",
        "top_types:",
    ]
    for k, v in TOP_LABELS.items():
        lines.append(f"  - {{id: {k}, label: {v}}}")
    lines += ["", "# 述語。group が同じなら近い（0.7）、polarity が逆なら 0。", "predicates:"]
    for pid, label, group, pol in PREDICATES:
        lines.append(f"  - {{id: {pid}, label: {q(label)}, group: {group}, polarity: {pol}}}")
    lines += ["", "# 中間概念は別名を持たない（本文照合には使わない）。parents は 1 つ。", "concepts:"]
    cur_top = None
    for c in cs:
        top = c["id"][0]
        if top != cur_top:
            lines.append(f"  # ---- {TOP_LABELS[top]} ----")
            cur_top = top
        if c["id"] in parents:
            lines.append(f"  - {{id: {c['id']}, label: {q(c['label'])}, parents: [{', '.join(c['parents'])}]}}")
        else:
            al = ", ".join(q(a) for a in c["aliases"])
            lines.append(f"  - {{id: {c['id']}, label: {q(c['label'])}, aliases: [{al}], parents: [{', '.join(c['parents'])}]}}")
    OUT_YAML.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return OUT_YAML


# ---------------------------------------------------------------------------
# データ
# ---------------------------------------------------------------------------
def load_split():
    """(開発用, 確認用, 分野の辞書)。doc_id の番号の奇数＝開発用、偶数＝確認用。"""
    import pandas as pd

    recs, _ = load_patent_csv(CSV)
    dom = dict(pd.read_csv(CSV, usecols=["doc_id", "domain"]).values)
    dev = [r for r in recs if int(r.doc_id[1:]) % 2 == 1]
    hold = [r for r in recs if int(r.doc_id[1:]) % 2 == 0]
    return dev, hold, dom


# ---------------------------------------------------------------------------
# 候補語の抽出（開発用だけ）
# ---------------------------------------------------------------------------
_NOISE = re.compile(r"[\"{}():\[\]/,≦≧=<>]|spayload|fkind|pubno|fnumber|ftype|JPpat|chemical|path|前記|上記|本発明|特許|請求")


def noun_phrases(text: str, tok) -> set[str]:
    """連続する名詞（数・非自立・代名詞を除く）の 1〜4 形態素の部分列を返す。"""
    out: set[str] = set()
    run: list[str] = []

    def flush() -> None:
        for i in range(len(run)):
            for j in range(i + 1, min(len(run), i + 4) + 1):
                p = "".join(run[i:j])
                if len(p) >= 2:
                    out.add(p)
        run.clear()

    for t in tok.tokenize(text):
        pos = t.part_of_speech.split(",")
        ok = (pos[0] == "名詞" and pos[1] not in ("数", "非自立", "代名詞", "接尾")) or (
            pos[0] == "名詞" and pos[1] == "接尾" and run
        ) or (pos[0] == "接頭詞" and not run)
        if ok and not re.fullmatch(r"[\d.\-~〜%]+", t.surface):
            run.append(t.surface)
        else:
            flush()
    flush()
    return out


def candidates() -> Path:
    """段落（課題・解決手段・用途）ごとの文書頻度と、他の段落に対する偏り（対数比）を数える。"""
    import pandas as pd
    from janome.tokenizer import Tokenizer

    dev, _, _ = load_split()
    tok = Tokenizer()
    df = {s: collections.Counter() for s in SECTIONS}
    n = {s: 0 for s in SECTIONS}
    for r in dev:
        for s in SECTIONS:
            b = r.sections.get(s, "")
            if b:
                n[s] += 1
                df[s].update(noun_phrases(b, tok))
    rows = []
    for s in SECTIONS:
        others = [o for o in SECTIONS if o != s]
        for p, c in df[s].items():
            if c < 3 or _NOISE.search(p):
                continue
            rs = c / n[s]
            ro = sum(df[o][p] / n[o] for o in others) / len(others)
            lr = math.log((rs + 1e-3) / (ro + 1e-3))
            rows.append({"section": s, "term": p, "df": c, "rate": round(rs, 4), "rate_other": round(ro, 4),
                         "log_ratio": round(lr, 3), "score": round(rs * max(lr, 0.0), 5)})
    out = pd.DataFrame(rows).sort_values(["section", "score", "df"], ascending=[True, False, False])
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / "candidates_dev.csv"
    out.to_csv(path, index=False)
    print(f"候補語 {len(out)} 行 → {path}（開発用 {len(dev)} 件、段落ごとの文書数 {n}）")
    return path


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------
SIM_PAIRS = [
    # 兄弟（同じ中間概念の下）
    ("M_zeolite_cha", "M_zeolite_mfi"),
    ("M_activated_carbon", "M_graphite"),
    ("M_alumina", "M_zirconia"),
    ("M_hydrothermal", "M_solgel"),
    ("M_gas_activation", "M_chemical_activation"),
    ("P_adsorption_capacity", "P_selectivity"),
    ("P_capacity", "P_cycle"),
    ("P_thermal_stability", "P_water_stability"),
    ("P_cost", "P_productivity"),
    ("U_lib", "U_solid_state_battery"),
    ("U_vehicle_exhaust", "U_water_treatment"),
    ("U_co2_capture", "U_gas_separation"),
    # 上位型だけ共有（別の中間概念）
    ("M_zeolite_cha", "M_activated_carbon"),
    ("M_hydrothermal", "M_calcination"),
    ("P_adsorption_capacity", "P_cost"),
    ("P_capacity", "P_thermal_stability"),
    ("U_lib", "U_vehicle_exhaust"),
    ("U_co2_capture", "U_medical"),
    # 上位型が違う
    ("M_zeolite_cha", "P_selectivity"),
    ("P_cycle", "U_lib"),
]


def coverage(recs, onto: Ontology, dom: dict) -> dict:
    """段落ごとに、対応する上位型の概念が 0 件の文書の割合と、拾えた概念数（異なり）の平均。"""
    ex = SectionExtractor(onto)
    per = []
    for r in recs:
        ms = ex.mentions(r)
        row = {"doc_id": r.doc_id, "domain": dom[r.doc_id]}
        for s, top in SECTIONS.items():
            body = r.sections.get(s, "")
            cids = {m.cid for m in ms if m.section == s and m.top == top}
            allc = {m.cid for m in ms if m.section == s}
            row[f"{s}_empty"] = not body
            row[f"{s}_n"] = len(cids)
            row[f"{s}_n_all"] = len(allc)
        per.append(row)

    def summarize(rows) -> dict:
        out = {"n_docs": len(rows)}
        for s, top in SECTIONS.items():
            ne = [x for x in rows if not x[f"{s}_empty"]]
            zero_ne = sum(1 for x in ne if x[f"{s}_n"] == 0)
            zero_all = sum(1 for x in rows if x[f"{s}_n"] == 0)
            out[s] = {
                "type": top,
                "n_nonempty": len(ne),
                "zero_rate_nonempty": round(zero_ne / len(ne), 4) if ne else None,
                "zero_rate_all": round(zero_all / len(rows), 4) if rows else None,
                "mean_concepts_nonempty": round(sum(x[f"{s}_n"] for x in ne) / len(ne), 2) if ne else None,
                "mean_concepts_any_type_nonempty": round(sum(x[f"{s}_n_all"] for x in ne) / len(ne), 2) if ne else None,
            }
        return out

    res = {"all": summarize(per), "by_domain": {}}
    for d in sorted({x["domain"] for x in per}):
        res["by_domain"][d] = summarize([x for x in per if x["domain"] == d])
    return res, per


def alias_stats(recs, onto: Ontology) -> list[dict]:
    """開発用で、表記ごとに段落別の文書頻度を数える（最長一致後の実際の出現）。"""
    ex = SectionExtractor(onto)
    cnt: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for r in recs:
        seen = set()
        for m in ex.mentions(r):
            k = (m.surface.lower(), m.cid, m.section)
            if k in seen:
                continue
            seen.add(k)
            cnt[(unicodedata.normalize("NFKC", m.surface).lower(), m.cid)][m.section] += 1
    rows = []
    for s, cid in ex.forms:
        c = cnt.get((s, cid), collections.Counter())
        rows.append({"form": s, "concept": cid, "top": onto.top_type(cid), "problem": c["problem"],
                     "solution": c["solution"], "use": c["use"], "total": sum(c.values())})
    return rows


def zero_docs_report(recs, per, dom, tok) -> list[dict]:
    """開発用で、対応する上位型が 0 件の段落の名詞句（語を足す手がかり）。"""
    by_id = {r.doc_id: r for r in recs}
    rows = []
    for x in per:
        for s in SECTIONS:
            if x[f"{s}_empty"] or x[f"{s}_n"] > 0:
                continue
            body = by_id[x["doc_id"]].sections[s]
            rows.append({"doc_id": x["doc_id"], "domain": dom[x["doc_id"]], "section": s,
                         "head": body[:160]})
    return rows


def validate(write_zero: bool = True, dev_only: bool = False) -> dict:
    """読み込み・類似度・出現率を測り、_work/ontology_build/ に書く。"""
    import pandas as pd

    onto = Ontology.load(OUT_YAML)
    cs = concepts_data()
    parents = {p for c in cs for p in c["parents"]}
    counts = {}
    for t in TOP_LABELS:
        sub = [c for c in cs if c["id"].startswith(t)]
        counts[t] = {
            "intermediate": sum(1 for c in sub if c["id"] in parents),
            "leaf": sum(1 for c in sub if c["id"] not in parents),
            "aliases": sum(len(c["aliases"]) for c in sub),
            "surface_forms": sum(len(c["aliases"]) + (0 if c["id"] in parents else 1) for c in sub),
        }
    sims = [{"a": a, "b": b, "a_label": onto.label(a), "b_label": onto.label(b), "sim": round(onto.concept_sim(a, b), 4)} for a, b in SIM_PAIRS]
    # 兄弟の全組・上位型だけ共有の全組の最小値・最大値
    leaves = [c["id"] for c in cs if c["id"] not in parents]
    par = {c["id"]: c["parents"][0] for c in cs}
    sib, cross = [], []
    for i, a in enumerate(leaves):
        for b in leaves[i + 1:]:
            if a[0] != b[0]:
                continue
            v = onto.concept_sim(a, b)
            (sib if par[a] == par[b] else cross).append(v)
    dev, hold, dom = load_split()
    cov_dev, per_dev = coverage(dev, onto, dom)
    cov_hold = None if dev_only else coverage(hold, onto, dom)[0]
    res = {
        "version": onto.version,
        "load_ok": True,
        "n_concepts_in_ontology_object": len(onto.concepts),
        "counts": counts,
        "sim_pairs": sims,
        "sim_all_sibling_leaf_pairs": {"n": len(sib), "min": round(min(sib), 4), "max": round(max(sib), 4)},
        "sim_all_cross_intermediate_leaf_pairs": {"n": len(cross), "min": round(min(cross), 4), "max": round(max(cross), 4)},
        "coverage_dev": cov_dev,
        "coverage_holdout": cov_hold,
    }
    WORK.mkdir(parents=True, exist_ok=True)
    ast = pd.DataFrame(alias_stats(dev, onto))
    ast.to_csv(WORK / "alias_stats_dev.csv", index=False)
    if write_zero:
        pd.DataFrame(zero_docs_report(dev, per_dev, dom, None)).to_csv(WORK / "uncovered_sections_dev.csv", index=False)
    res["alias_zero_hit_dev"] = int((ast.total == 0).sum())
    res["alias_total"] = int(len(ast))
    name = "validation_dev_only.json" if dev_only else "validation.json"
    (WORK / name).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def print_summary(res: dict) -> None:
    print("version", res["version"], "counts", json.dumps(res["counts"], ensure_ascii=False))
    for k in ("coverage_dev", "coverage_holdout"):
        if res[k] is None:
            continue
        a = res[k]["all"]
        print(k, {s: (a[s]["zero_rate_nonempty"], a[s]["zero_rate_all"], a[s]["mean_concepts_nonempty"]) for s in SECTIONS})
        for d, v in res[k]["by_domain"].items():
            print("   ", d, {s: v[s]["zero_rate_nonempty"] for s in SECTIONS})
    print("sib", res["sim_all_sibling_leaf_pairs"], "cross", res["sim_all_cross_intermediate_leaf_pairs"])
    print("alias zero-hit (dev)", res["alias_zero_hit_dev"], "/", res["alias_total"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["candidates", "build", "validate", "all"])
    ap.add_argument("--dev-only", action="store_true", help="語を足す作業中は開発用だけを測る（確認用を見ない）")
    a = ap.parse_args()
    if a.step in ("candidates", "all"):
        candidates()
    if a.step in ("build", "all"):
        print("書き出し →", build_yaml())
    if a.step in ("validate", "all"):
        print_summary(validate(dev_only=a.dev_only))


if __name__ == "__main__":
    main()
