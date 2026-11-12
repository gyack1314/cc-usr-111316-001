"""演示：进口中间品、零产出空部门、近奇异链条与非法输入各自得到什么结论。

只打印结论摘要，不写出任何分析包；run 命令才负责保存。
"""
import json
from fractions import Fraction
from pathlib import Path
from .io import load
from .mrio import run_analysis, split_sector, merge_sectors


def _show_package(pkg):
    print(f'状态：{pkg["status"]}')
    if pkg['status'] != 'ok':
        for b in pkg['blocks']:
            print(f'  正式结论被阻止：{b}')
        ev = pkg['solve_evidence']
        print(f'  证据：Hawkins-Simon 主元={ev.get("hawkins_simon_pivots")}，'
              f'条件数={ev.get("condition_number_1")}（上限 {ev.get("condition_limit")}）')
        print('  正式结论（责任/情景/弹性）一律为 null，只保留诊断证据。')
        return
    removed = [m for m in pkg['sector_mapping'] if not m['kept']]
    for m in removed:
        print(f'  空部门剔除：{m["id"]}（{m["reason"]}）')
    resp = pkg['responsibilities']
    for dest, blk in resp['per_destination'].items():
        print(f'  目的地 {dest}：总足迹 {blk["total"]} ≈ {float(Fraction(blk["total"])):.4f}'
              f'（直接 {blk["direct"]}，间接 {blk["indirect"]}）')
        for sec, val in blk['by_sector'].items():
            print(f'    责任 {sec}: {val}')
    cons = resp['emissions_conservation']
    print(f'  排放守恒核对：各目的地合计 {cons["grand_total"]} == 保留部门直接排放合计 '
          f'{cons["sum_direct_emissions_kept"]} → {cons["consistent"]}（无重复归算）')
    sc = pkg['scenario']
    if sc:
        print(f'  提取情景 {sc["cells"]}（{sc["status"]}）：结构反事实，非因果预测')
        for dest, blk in (sc['per_destination'] or {}).items():
            print(f'    {dest}: {blk["before"]} → {blk["after"]}（Δ {blk["delta"]}）')
    dv = pkg['derivative']
    fd = dv['fd_verification']
    print(f'  方向导数：{json.dumps({k: v["dF_dt"] for k, v in dv["per_destination"].items()}, ensure_ascii=False)}')
    print(f'  差分复核：最细可行步长 {fd["finest_feasible_h"]}，弹性有效={fd["valid"]}'
          + (f'（{fd["note"]}）' if fd['note'] else ''))


def demo(data_dir):
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        print(f'资料目录不存在：{data_dir}')
        return 1
    print('== 正常资料：进口中间品沿产业链的责任 + 零产出空部门 ==')
    v, h = load(data_dir / 'normal.json')
    pkg = run_analysis(v, manifest_sha256=h)
    _show_package(pkg)
    print()
    print('== 拆分汇回一致性：north-equipment 按 1/3 拆成两个同比例子部门 ==')
    split = split_sector(v, 0, '1/3')
    pkg_split = run_analysis(split)
    same = all(pkg_split['responsibilities']['per_destination'][d]['total']
               == pkg['responsibilities']['per_destination'][d]['total']
               for d in pkg['responsibilities']['per_destination'])
    merged = merge_sectors(split, [[0, 1], [2], [3]])
    roundtrip = run_analysis(merged)['responsibilities']['grand_total'] == \
        pkg['responsibilities']['grand_total']
    print(f'  拆分后各目的地总足迹与原表精确相等：{same}；汇回后总量一致：{roundtrip}')
    print()
    print('== 近奇异链条：生产性成立但病态 ==')
    v, h = load(data_dir / 'near-singular-chain.json')
    _show_package(run_analysis(v, manifest_sha256=h))
    print()
    print('== 非法输入：不留下看似成功的分析包 ==')
    for name in ('zero-output-positive-emissions.json', 'invalid-config.json'):
        try:
            v, h = load(data_dir / name)
            run_analysis(v, manifest_sha256=h)
            print(f'  {name}: 意外通过（应为输入非法）')
            return 1
        except ValueError as exc:
            print(f'  {name}: 输入非法 → {exc}；未生成分析包')
    return 0
