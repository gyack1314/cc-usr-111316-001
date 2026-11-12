"""MRIO 核算：精确足迹、生产性证据、情景、导数复核、拆分汇回与失败路径。"""
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from analysis_data.io import load
from analysis_data.mrio import (MrioError, load_model, build_system, run_analysis,
                                split_sector, merge_sectors)

ROOT = Path(__file__).resolve().parent.parent


def normal():
    return json.loads((ROOT / 'data' / 'normal.json').read_text(encoding='utf8'))


def near_singular():
    return json.loads((ROOT / 'data' / 'near-singular-chain.json').read_text(encoding='utf8'))


class ValidationTests(unittest.TestCase):
    def test_balance_discrepancy_rejected(self):
        v = normal()
        v['x'][0] = '101'
        with self.assertRaises(MrioError):
            load_model(v)

    def test_accounting_discrepancies_recorded_zero(self):
        pkg = run_analysis(normal())
        self.assertEqual(set(pkg['accounting']['discrepancies'].values()), {'0'})

    def test_negative_entry_rejected(self):
        v = normal()
        v['Z'][0][1] = '-1'
        with self.assertRaises(MrioError):
            load_model(v)

    def test_missing_is_not_zero(self):
        for mutate in (
                lambda v: v.pop('f'),
                lambda v: v['Y'][0].__setitem__(0, None),
                lambda v: v['Z'][0].__setitem__(0, 1.5),
                lambda v: v.pop('numeric_policy')):
            v = normal()
            mutate(v)
            with self.subTest(mutate=mutate):
                with self.assertRaises(MrioError):
                    load_model(v)

    def test_rational_forms(self):
        v = normal()
        v['f'][0] = '2.5'      # 十进制精确串
        v['f'][1] = '45/2'     # 分数串
        m = load_model(v)
        self.assertEqual(m.f[0], Fraction(5, 2))
        self.assertEqual(m.f[1], Fraction(45, 2))
        v = normal()
        v['f'][0] = 'abc'
        with self.assertRaises(MrioError):
            load_model(v)

    def test_empty_sector_removed_with_mapping(self):
        pkg = run_analysis(normal())
        mapping = {m['id']: m for m in pkg['sector_mapping']}
        self.assertFalse(mapping['empty']['kept'])
        self.assertIn('零产出', mapping['empty']['reason'])
        self.assertTrue(mapping['north-equipment']['kept'])
        self.assertEqual(mapping['south-power']['kept_index'], 1)

    def test_zero_output_with_emissions_rejected(self):
        v, _ = load(ROOT / 'data' / 'zero-output-positive-emissions.json')
        with self.assertRaises(MrioError):
            run_analysis(v)

    def test_zero_output_with_column_flow_rejected(self):
        v = normal()
        v['Z'][0][2] = '0'   # 行仍全零
        v['Z'][1][2] = '5'   # 但空部门列有流入 → 不能构造 A
        v['Z'][1][1] = '0'
        v['x'][1] = '75'     # 恢复会计平衡
        with self.assertRaises(MrioError):
            run_analysis(v)


class FootprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = run_analysis(normal())

    def test_exact_totals(self):
        per = self.pkg['responsibilities']['per_destination']
        self.assertEqual(per['north']['total'], '345/19')
        self.assertEqual(per['south']['total'], '225/19')
        self.assertEqual(per['north']['direct'], '27/2')
        self.assertEqual(per['north']['indirect'], '177/38')
        self.assertEqual(per['south']['direct'], '19/2')
        self.assertEqual(per['south']['indirect'], '89/38')
        self.assertEqual(self.pkg['responsibilities']['grand_total'], '30')

    def test_by_sector_sums_to_total(self):
        for dest, blk in self.pkg['responsibilities']['per_destination'].items():
            self.assertEqual(sum(map(Fraction, blk['by_sector'].values())),
                             Fraction(blk['total']))
            self.assertEqual(Fraction(blk['direct']) + Fraction(blk['indirect']),
                             Fraction(blk['total']))

    def test_layers_are_increments_not_double_counted(self):
        resp = self.pkg['responsibilities']
        for dest, blk in resp['layers'].items():
            total = Fraction(resp['per_destination'][dest]['total'])
            acc = sum(map(Fraction, blk['increments'])) + Fraction(blk['remainder'])
            self.assertEqual(acc, total)
            # 第0层即直接部分
            self.assertEqual(Fraction(blk['increments'][0]),
                             Fraction(resp['per_destination'][dest]['direct']))

    def test_emissions_conservation(self):
        cons = self.pkg['responsibilities']['emissions_conservation']
        self.assertTrue(cons['consistent'])
        self.assertEqual(cons['grand_total'], cons['sum_direct_emissions_kept'])

    def test_solve_evidence_exact(self):
        ev = self.pkg['solve_evidence']
        self.assertEqual(ev['hawkins_simon_pivots'], ['1', '19/20'])
        self.assertEqual(ev['condition_number_1'], '125/76')
        self.assertEqual(ev['residual_max_abs'], '0')
        self.assertTrue(ev['leontief_nonnegative'])
        self.assertEqual(ev['leontief_inverse'][0], ['20/19', '5/19'])
        # 谱半径真值 sqrt(1/20)，Gelfand 估计应接近
        self.assertAlmostEqual(ev['spectral_radius_estimate'], 0.2236067977, places=6)


class BlockingTests(unittest.TestCase):
    def test_near_singular_blocked(self):
        pkg = run_analysis(near_singular())
        self.assertEqual(pkg['status'], 'blocked')
        self.assertIsNone(pkg['responsibilities'])
        self.assertIsNone(pkg['scenario'])
        self.assertIsNone(pkg['derivative'])
        self.assertIn('1999999', pkg['blocks'][0])
        ev = pkg['solve_evidence']
        # 生产性证据仍在：主元全正，但条件数 1999999 超过上限 10000
        self.assertEqual(ev['hawkins_simon_pivots'], ['1', '1999999/1000000000000'])
        self.assertEqual(ev['condition_number_1'], '1999999')

    def test_balanced_but_nonproductive_blocked(self):
        # 会计平衡但 rho(A)=1：平衡通过不等于可解
        v = {
            'kind': 'mrio', 'sectors': ['a', 'b'], 'destinations': ['d'],
            'Z': [['0', '50'], ['50', '0']], 'x': ['50', '50'],
            'Y': [['0'], ['0']], 'f': ['5', '5'],
            'currency_unit': 'u', 'extraction_cells': [],
            'direction': [['0', '0'], ['0', '0']],
            'numeric_policy': {'rational_or_residual_tolerance': '1/1000000000',
                               'condition_limit': 1000000},
        }
        load_model(v)  # 平衡校验通过
        pkg = run_analysis(v)
        self.assertEqual(pkg['status'], 'blocked')
        self.assertIn('奇异', pkg['blocks'][0])
        self.assertIsNone(pkg['responsibilities'])


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = run_analysis(normal())

    def test_extraction_counterfactual(self):
        sc = self.pkg['scenario']
        self.assertEqual(sc['status'], 'ok')
        self.assertEqual(sc['cells'], [[0, 1]])
        self.assertIn('非因果预测', sc['interpretation'])
        self.assertEqual(sc['per_destination']['north']['before'], '345/19')
        self.assertEqual(sc['per_destination']['north']['after'], '33/2')
        self.assertEqual(sc['per_destination']['north']['delta'], '-63/38')
        self.assertEqual(sc['per_destination']['south']['after'], '21/2')
        self.assertEqual(sc['grand_total']['after'], '27')

    def test_scenario_resolves_output(self):
        req = self.pkg['scenario']['per_destination']['north']
        self.assertEqual(req['requirement_before']['north-equipment'], '1350/19')
        self.assertEqual(req['requirement_after']['north-equipment'], '60')
        self.assertEqual(req['requirement_after']['south-power'], '42')

    def test_scenario_productivity_evidence_kept(self):
        ev = self.pkg['scenario']['evidence']
        self.assertTrue(ev['leontief_nonnegative'])
        self.assertEqual(ev['residual_max_abs'], '0')

    def test_extraction_cell_on_removed_sector_rejected(self):
        v = normal()
        v['extraction_cells'] = [[0, 2]]
        with self.assertRaises(MrioError):
            run_analysis(v)


class DerivativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = run_analysis(normal())

    def test_analytic_derivative_exact(self):
        per = self.pkg['derivative']['per_destination']
        self.assertEqual(per['north']['dF_dt'], '2520/361')
        self.assertEqual(per['south']['dF_dt'], '2040/361')
        self.assertEqual(self.pkg['derivative']['grand_total_dF_dt'], '240/19')
        self.assertEqual(per['north']['elasticity_per_unit_t'], '168/437')

    def test_fd_verification_converges(self):
        fd = self.pkg['derivative']['fd_verification']
        self.assertTrue(fd['valid'])
        self.assertEqual(fd['finest_feasible_h'], '1/1000000')
        self.assertEqual(len(fd['steps']), 6)
        errors = []
        for step in fd['steps']:
            self.assertEqual(step['mode'], 'central')
            errors.append({d: Fraction(e) for d, e in step['abs_error_vs_analytic'].items()})
        # 误差随步长递减（中心差分 O(h^2)），最细步长进入容差
        for dest in errors[0]:
            seq = [e[dest] for e in errors]
            self.assertTrue(all(a > b for a, b in zip(seq, seq[1:]) if b != 0))
            self.assertLessEqual(seq[-1], Fraction(fd['tolerance']))

    def test_feasible_interval_reported(self):
        lo, hi = self.pkg['derivative']['feasible_interval_nonnegative']
        self.assertEqual(lo, '-1/4')   # A_01=1/4，D_01=1
        self.assertIsNone(hi)

    def test_infeasible_steps_excluded_from_validity(self):
        v = normal()
        v['direction'][0][1] = '-3'   # h=1/10 正向：A_01=1/4-3/10<0
        pkg = run_analysis(v)
        fd = pkg['derivative']['fd_verification']
        self.assertFalse(fd['steps'][0]['plus_feasible'])   # 过大扰动跨到不可行域
        self.assertEqual(fd['steps'][0]['mode'], 'one_sided')
        self.assertEqual(fd['steps'][1]['mode'], 'central')  # 更细步长回到可行域
        self.assertTrue(fd['valid'])
        self.assertIn('不可行域', fd['note'])

    def test_no_valid_elasticity_when_all_steps_infeasible(self):
        v = {
            'kind': 'mrio', 'sectors': ['a', 'b'], 'destinations': ['d'],
            'Z': [['0', '0'], ['20', '0']], 'x': ['40', '100'],
            'Y': [['40'], ['80']], 'f': ['4', '10'],
            'currency_unit': 'u', 'extraction_cells': [],
            'direction': [['0', '1'], ['0', '0']],
            'numeric_policy': {'rational_or_residual_tolerance': '1/1000000000',
                               'condition_limit': '9/4'},  # 恰为基线条件数
        }
        pkg = run_analysis(v)
        self.assertEqual(pkg['status'], 'ok')
        dv = pkg['derivative']
        # 解析导数仍给出
        self.assertIn('dF_dt', dv['per_destination']['d'])
        # 正向扰动条件数超限、负向扰动系数为负：所有步长不可行 → 不报有效弹性
        self.assertFalse(dv['fd_verification']['valid'])
        self.assertIsNone(dv['fd_verification']['finest_feasible_h'])
        self.assertIsNone(dv['per_destination']['d']['elasticity_per_unit_t'])
        self.assertIn('不可行域', dv['fd_verification']['note'])


class SplitMergeTests(unittest.TestCase):
    def test_split_merge_roundtrip_table(self):
        v = normal()
        split = split_sector(v, 0, '1/3')
        self.assertEqual(split['sectors'][:2], ['north-equipment#1', 'north-equipment#2'])
        merged = merge_sectors(split, [[0, 1], [2], [3]])
        m0, m1 = load_model(v), load_model(merged)
        self.assertEqual(m0.Z, m1.Z)
        self.assertEqual(m0.x, m1.x)
        self.assertEqual(m0.Y, m1.Y)
        self.assertEqual(m0.f, m1.f)
        self.assertEqual(m0.direction, m1.direction)
        self.assertEqual(m0.extraction_cells, m1.extraction_cells)

    def test_split_footprint_invariant(self):
        v = normal()
        base = run_analysis(v)['responsibilities']
        split = run_analysis(split_sector(v, 0, '1/3'))['responsibilities']
        for dest in ('north', 'south'):
            self.assertEqual(split['per_destination'][dest]['total'],
                             base['per_destination'][dest]['total'])
            # 两个子部门责任汇回等于原部门责任
            agg = Fraction(split['per_destination'][dest]['by_sector']['north-equipment#1']) \
                + Fraction(split['per_destination'][dest]['by_sector']['north-equipment#2'])
            self.assertEqual(agg, Fraction(base['per_destination'][dest]['by_sector']['north-equipment']))
        self.assertEqual(split['grand_total'], base['grand_total'])

    def test_split_share_bounds(self):
        v = normal()
        for bad in ('0', '1', '-1/2', '3/2'):
            with self.subTest(share=bad):
                with self.assertRaises(MrioError):
                    split_sector(v, 0, bad)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'analysis_data.cli', *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_run_writes_package(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'nested' / 'pkg.json'
            r = self.run_cli('run', 'data/normal.json', '-o', str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            pkg = json.loads(out.read_text(encoding='utf8'))
            for key in ('sector_mapping', 'accounting', 'solve_evidence',
                        'responsibilities', 'scenario', 'derivative', 'manifest_sha256'):
                self.assertIn(key, pkg)
            self.assertEqual(pkg['status'], 'ok')

    def test_run_blocked_exit2_but_package_marked(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'pkg.json'
            r = self.run_cli('run', 'data/near-singular-chain.json', '-o', str(out))
            self.assertEqual(r.returncode, 2, r.stderr)
            pkg = json.loads(out.read_text(encoding='utf8'))
            self.assertEqual(pkg['status'], 'blocked')
            self.assertIsNone(pkg['responsibilities'])

    def test_invalid_input_leaves_no_package(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('invalid-config.json', 'zero-output-positive-emissions.json'):
                out = Path(d) / (name + '.out')
                r = self.run_cli('run', f'data/{name}', '-o', str(out))
                self.assertEqual(r.returncode, 1, name)
                self.assertFalse(out.exists(), f'{name} 不应留下看似成功的分析包')

    def test_demo_exit0(self):
        r = self.run_cli('demo')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('近奇异', r.stdout)
        self.assertIn('未生成分析包', r.stdout)


if __name__ == '__main__':
    unittest.main()
