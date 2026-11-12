"""多区域投入产出(MRIO)隐含排放核算。

口径：
- Z行供给、列使用，Y行供给、列目的地，f直接排放，x总产出，同一币值单位。
- 全部量以精确有理数(Fraction)核算；A=Z·diag(x)^{-1}，L=(I-A)^{-1}。
- 每个供给部门须满足 rowsum(Z)+rowsum(Y)==x（精确会计平衡），缺项不等于0。
- 零产出部门仅在交易、最终需求、排放全零时剔除并保留映射；不得除零或以常数填补。
- 生产性以 Hawkins-Simon 主元全正且 Leontief 逆非负为证；条件数超上限或
  非生产性时阻止正式结论（status=blocked），只保留证据。
- 提取情景把登记的A单元置零重新求解：这是假设投入被无排放替代的结构反事实，
  不是因果预测，也不要求情景仍满足原会计恒等式。
- 方向导数按 A(t)=A+t·D 在 t=0 处取解析导数，并以步长递减的差分复核；
  跨到不可行域（负系数、非生产、超条件数上限）的步长不参与弹性有效性判断。
"""
from dataclasses import dataclass, field
from fractions import Fraction
import math

LAYERS = 8  # 层级分解展示的层数，余量精确给出


class MrioError(ValueError):
    """资料违反领域口径：缺项、负量、会计不平衡、空部门条件不满足、形状错误等。"""


class _Singular(Exception):
    """精确算术下矩阵奇异。"""


# ---------------------------------------------------------------- 解析与校验

def _rational(value, field_name):
    """把 JSON 量解析为精确有理数；缺项(null)或浮点一律拒绝，缺项不等于0。"""
    if isinstance(value, bool) or value is None:
        raise MrioError(f'{field_name}: 缺项或类型不当，缺项不等于0')
    if isinstance(value, int):
        return Fraction(value)
    if not isinstance(value, str):
        raise MrioError(f'{field_name}: 量必须为精确有理字符串，收到 {type(value).__name__}')
    try:
        return Fraction(value.strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise MrioError(f'{field_name}: 无法解析为有理数: {value!r}') from exc


def _nonneg(value, field_name):
    q = _rational(value, field_name)
    if q < 0:
        raise MrioError(f'{field_name}: 负量 {value!r} 违反非负口径')
    return q


def _unique_ids(items, field_name):
    seen = set()
    for x in items:
        if not isinstance(x, str) or not x or x in seen:
            raise MrioError(f'{field_name}: 身份缺失或重复')
        seen.add(x)
    return seen


@dataclass
class Model:
    """通过校验的原始口径资料（全精确有理数）。"""
    sectors: list
    destinations: list
    Z: list            # n×n Fraction
    x: list            # n Fraction
    Y: list            # n×k Fraction
    f: list            # n Fraction
    currency_unit: str
    extraction_cells: list   # [(i,j)] 原始坐标
    direction: list          # n×n Fraction（允许负元）
    tolerance: Fraction
    condition_limit: Fraction
    discrepancies: list = field(default_factory=list)  # 会计差异，全零才到此


_REQUIRED = ('kind', 'sectors', 'destinations', 'Z', 'x', 'Y', 'f',
             'currency_unit', 'extraction_cells', 'direction', 'numeric_policy')


def load_model(v):
    """校验并解析资料为 Model；任何缺项、负量、不平衡都抛 MrioError。"""
    if not isinstance(v, dict):
        raise MrioError('顶层必须为对象')
    for key in _REQUIRED:
        if key not in v:
            raise MrioError(f'缺少键 {key!r}：缺项不等于0')
    if v['kind'] != 'mrio':
        raise MrioError(f"kind 必须为 'mrio'，收到 {v['kind']!r}")
    sectors, destinations = v['sectors'], v['destinations']
    if not isinstance(sectors, list) or not isinstance(destinations, list) \
            or not sectors or not destinations:
        raise MrioError('sectors/destinations 必须为非空数组')
    _unique_ids(sectors, 'sectors')
    _unique_ids(destinations, 'destinations')
    n, k = len(sectors), len(destinations)

    def matrix(raw, rows, cols, name, signed=False):
        if not isinstance(raw, list) or len(raw) != rows:
            raise MrioError(f'{name}: 行数应为 {rows}')
        parse = _rational if signed else _nonneg
        out = []
        for i, row in enumerate(raw):
            if not isinstance(row, list) or len(row) != cols:
                raise MrioError(f'{name}: 第 {i} 行长度应为 {cols}')
            out.append([parse(c, f'{name}[{i}][{j}]') for j, c in enumerate(row)])
        return out

    Z = matrix(v['Z'], n, n, 'Z')
    Y = matrix(v['Y'], n, k, 'Y')
    direction = matrix(v['direction'], n, n, 'direction', signed=True)
    x = [_nonneg(c, f'x[{i}]') for i, c in enumerate(v['x'])] if isinstance(v['x'], list) and len(v['x']) == n else None
    if x is None:
        raise MrioError(f'x: 长度应为 {n}')
    f = [_nonneg(c, f'f[{i}]') for i, c in enumerate(v['f'])] if isinstance(v['f'], list) and len(v['f']) == n else None
    if f is None:
        raise MrioError(f'f: 长度应为 {n}')
    unit = v['currency_unit']
    if not isinstance(unit, str) or not unit:
        raise MrioError('currency_unit: 必须为非空字符串（统一币值单位）')

    cells = v['extraction_cells']
    if not isinstance(cells, list):
        raise MrioError('extraction_cells: 必须为数组')
    norm_cells = []
    for c in cells:
        if not isinstance(c, list) or len(c) != 2 or any(type(t) is not int for t in c):
            raise MrioError('extraction_cells: 每个单元须为 [i,j] 整数对')
        i, j = c
        if not (0 <= i < n and 0 <= j < n):
            raise MrioError(f'extraction_cells: 单元 {c} 越界')
        if (i, j) in norm_cells:
            raise MrioError(f'extraction_cells: 单元 {c} 重复登记')
        norm_cells.append((i, j))

    policy = v['numeric_policy']
    if not isinstance(policy, dict):
        raise MrioError('numeric_policy: 必须为对象')
    for key in ('rational_or_residual_tolerance', 'condition_limit'):
        if key not in policy:
            raise MrioError(f'numeric_policy: 缺少 {key!r}')
    tol = _rational(policy['rational_or_residual_tolerance'], 'tolerance')
    limit = _rational(policy['condition_limit'], 'condition_limit')
    if tol <= 0 or limit <= 0:
        raise MrioError('numeric_policy: 容差与条件数上限必须为正')

    # 会计平衡：每个供给部门 rowsum(Z)+rowsum(Y)==x，精确判定。
    discrepancies = []
    bad = []
    for i in range(n):
        d = sum(Z[i]) + sum(Y[i]) - x[i]
        discrepancies.append(d)
        if d != 0:
            bad.append(f'{sectors[i]}: 差异 {d}')
    if bad:
        raise MrioError('会计不平衡（rowsum(Z)+rowsum(Y)!=x）：' + '；'.join(bad))

    return Model(sectors, destinations, Z, x, Y, f, unit, norm_cells,
                 direction, tol, limit, discrepancies)


# ---------------------------------------------------------------- 空部门剔除

@dataclass
class System:
    """剔除合法空部门后的可核算系统。"""
    model: Model
    kept: list           # 原始索引 → 保留
    mapping: list        # 部门映射证据
    A: list              # m×m
    g: list              # m 直接强度 f_i/x_i
    Y: list              # m×k
    direction: list      # m×m
    cells: list          # [(i,j)] 保留坐标
    kept_ids: list = None   # 保留部门 id
    L: list = None          # 基线 Leontief 逆（run_analysis 填入）


def build_system(model):
    n = len(model.sectors)
    kept, mapping = [], []
    for i in range(n):
        if model.x[i] > 0:
            mapping.append({'index': i, 'id': model.sectors[i], 'kept': True, 'kept_index': len(kept)})
            kept.append(i)
            continue
        # 零产出部门：仅当交易(行与列)、最终需求、排放全零才可剔除。
        if any(model.Z[i][j] != 0 for j in range(n)) \
                or any(model.Z[j][i] != 0 for j in range(n)) \
                or any(model.Y[i]) or model.f[i] != 0:
            raise MrioError(
                f'零产出部门 {model.sectors[i]!r} 存在非零交易、最终需求或排放，'
                '不能剔除，也不允许除零或以小常数填补')
        mapping.append({'index': i, 'id': model.sectors[i], 'kept': False,
                        'reason': '零产出且交易、最终需求、排放全零，剔除并保留映射'})
    if not kept:
        raise MrioError('没有正产出部门，无可核算子系统')
    pos = {old: new for new, old in enumerate(kept)}
    A = [[model.Z[gi][gj] / model.x[gj] for gj in kept] for gi in kept]
    g = [model.f[gi] / model.x[gi] for gi in kept]
    Y = [model.Y[gi][:] for gi in kept]
    for i in range(n):
        for j in range(n):
            if (i not in pos or j not in pos) and model.direction[i][j] != 0:
                raise MrioError('direction 涉及已剔除的空部门，无法解释')
    direction = [[model.direction[gi][gj] for gj in kept] for gi in kept]
    cells = []
    for (i, j) in model.extraction_cells:
        if i not in pos or j not in pos:
            raise MrioError(f'extraction_cells: 单元 [{i},{j}] 涉及已剔除的空部门')
        cells.append((pos[i], pos[j]))
    return System(model, kept, mapping, A, g, Y, direction, cells,
                  kept_ids=[model.sectors[i] for i in kept])


# ---------------------------------------------------------------- 精确线性代数

def _eye(n):
    return [[Fraction(int(i == j)) for j in range(n)] for i in range(n)]


def _matmul(A, B):
    n, p, m = len(A), len(B), len(B[0])
    return [[sum(A[i][t] * B[t][j] for t in range(p)) for j in range(m)] for i in range(n)]


def _matvec(A, v):
    return [sum(A[i][j] * v[j] for j in range(len(v))) for i in range(len(A))]


def _lu_pivots(M):
    """不换主元 LU 的主元序列。Z 矩阵主元全正 ⇔ Hawkins-Simon 条件成立。"""
    n = len(M)
    a = [row[:] for row in M]
    pivots = []
    for c in range(n):
        d = a[c][c]
        if d == 0:
            raise _Singular
        pivots.append(d)
        for i in range(c + 1, n):
            if a[i][c]:
                w = a[i][c] / d
                for j in range(c + 1, n):
                    a[i][j] -= w * a[c][j]
    return pivots


def _inverse(M):
    """Gauss-Jordan 精确求逆。"""
    n = len(M)
    a = [row[:] + e for row, e in zip(M, _eye(n))]
    for c in range(n):
        p = next((i for i in range(c, n) if a[i][c] != 0), None)
        if p is None:
            raise _Singular
        a[c], a[p] = a[p], a[c]
        d = a[c][c]
        a[c] = [v / d for v in a[c]]
        for i in range(n):
            if i != c and a[i][c] != 0:
                w = a[i][c]
                a[i] = [u - w * v for u, v in zip(a[i], a[c])]
    return [row[n:] for row in a]


def _norm1(M):
    n = len(M)
    return max((sum(abs(M[i][j]) for i in range(n)) for j in range(n)), default=Fraction(0))


def _spectral_radius_estimate(A):
    """Gelfand 型浮点估计 ||A^m v||^{1/m}，仅供诊断；证明性证据是主元与 L 非负。"""
    n = len(A)
    a = [[float(v) for v in row] for row in A]
    v = [1.0] * n
    log_prod = 0.0
    steps = 0
    for _ in range(512):
        w = [sum(a[i][j] * v[j] for j in range(n)) for i in range(n)]
        norm = max(abs(t) for t in w)
        if norm == 0.0:
            return 0.0
        log_prod += math.log(norm)
        steps += 1
        v = [t / norm for t in w]
    return math.exp(log_prod / steps) if steps else 0.0


def _certificate(A, condition_limit):
    """生产性与数值证据。返回 (ok, L或None, 证据dict, 阻止原因或None)。"""
    n = len(A)
    M = [[Fraction(int(i == j)) - A[i][j] for j in range(n)] for i in range(n)]
    row_sums = [sum(row) for row in A]
    ev = {
        'spectral_radius_row_sum_bounds': [str(min(row_sums)), str(max(row_sums))],
        'spectral_radius_estimate': _spectral_radius_estimate(A),
    }
    try:
        pivots = _lu_pivots(M)
    except _Singular:
        ev['hawkins_simon_pivots'] = None
        return False, None, ev, '(I-A) 奇异：谱半径达到1，子系统非生产性'
    ev['hawkins_simon_pivots'] = [str(p) for p in pivots]
    if any(p <= 0 for p in pivots):
        return False, None, ev, 'Hawkins-Simon 主元非全正，子系统非生产性'
    L = _inverse(M)
    ev['leontief_nonnegative'] = all(v >= 0 for row in L for v in row)
    if not ev['leontief_nonnegative']:
        return False, None, ev, 'Leontief 逆含负元，子系统非生产性'
    cond = _norm1(M) * _norm1(L)
    ML = _matmul(M, L)
    residual = max((abs(ML[i][j] - Fraction(int(i == j))) for i in range(n) for j in range(n)),
                   default=Fraction(0))
    ev['condition_number_1'] = str(cond)
    ev['condition_number_1_float'] = float(cond)
    ev['residual_max_abs'] = str(residual)
    ev['leontief_inverse'] = [[str(v) for v in row] for row in L]
    if cond > condition_limit:
        return False, L, ev, f'条件数 {cond} 超过上限 {condition_limit}，矩阵近奇异'
    return True, L, ev, None


# ---------------------------------------------------------------- 足迹核算

def _footprints(L, g, Y, destinations, sector_ids):
    """按目的地：r_k=L·Y_k，责任 g_i·r_ik，总量及直接/间接分解。"""
    m, k = len(g), len(destinations)
    per = {}
    for c in range(k):
        y = [Y[i][c] for i in range(m)]
        r = _matvec(L, y)
        by_sector = [g[i] * r[i] for i in range(m)]
        total = sum(by_sector)
        direct = sum(g[i] * y[i] for i in range(m))
        per[destinations[c]] = {
            'requirement': {sector_ids[i]: str(r[i]) for i in range(m)},
            'by_sector': {sector_ids[i]: str(by_sector[i]) for i in range(m)},
            'total': str(total),
            'direct': str(direct),
            'indirect': str(total - direct),
        }
    return per


def _layers(A, g, Y, destinations, totals):
    """层级增量 c_m=g·A^m·Y_k（增量而非总乘数，不可重复累加）及精确余量。"""
    m, k = len(A), len(destinations)
    out = {}
    for c in range(k):
        v = [Y[i][c] for i in range(m)]
        layers = []
        acc = Fraction(0)
        for _ in range(LAYERS):
            c_m = sum(g[i] * v[i] for i in range(m))
            layers.append(str(c_m))
            acc += c_m
            v = _matvec(A, v)
        out[destinations[c]] = {
            'increments': layers,
            'remainder': str(Fraction(totals[destinations[c]]['total']) - acc),
        }
    return out


# ---------------------------------------------------------------- 情景与导数

def _scenario(system, evidence_limit):
    """提取情景：登记的A单元置零，其余A、g、Y固定，重新解产出与足迹。"""
    model = system.model
    A2 = [row[:] for row in system.A]
    for (i, j) in system.cells:
        A2[i][j] = Fraction(0)
    ok, L2, ev, reason = _certificate(A2, evidence_limit)
    result = {
        'type': 'extraction',
        'cells': [[i, j] for (i, j) in system.cells],
        'interpretation': '登记的A单元置零：假设该中间投入被无排放替代；其余A、直接强度、'
                          '最终需求固定。结构反事实，非因果预测；不要求情景满足原会计恒等式。',
        'evidence': ev,
    }
    if not ok:
        result['status'] = 'blocked'
        result['blocks'] = [reason]
        result['per_destination'] = None
        return result
    before = _footprints(system.L, system.g, system.Y, model.destinations, system.kept_ids)
    after = _footprints(L2, system.g, system.Y, model.destinations, system.kept_ids)
    per = {}
    for name in model.destinations:
        per[name] = {
            'before': before[name]['total'],
            'after': after[name]['total'],
            'delta': str(Fraction(after[name]['total']) - Fraction(before[name]['total'])),
            'requirement_before': before[name]['requirement'],
            'requirement_after': after[name]['requirement'],
        }
    result['status'] = 'ok'
    result['per_destination'] = per
    result['grand_total'] = {
        'before': str(sum(Fraction(before[d]['total']) for d in model.destinations)),
        'after': str(sum(Fraction(after[d]['total']) for d in model.destinations)),
    }
    result['grand_total']['delta'] = str(Fraction(result['grand_total']['after'])
                                         - Fraction(result['grand_total']['before']))
    return result


def _solve_total(A, g, Y):
    """给定 A 求各目的地总足迹（精确）。前置：A 已通过生产性判定。"""
    L = _inverse([[Fraction(int(i == j)) - A[i][j] for j in range(len(A))] for i in range(len(A))])
    m, k = len(A), len(Y[0])
    totals = []
    for c in range(k):
        r = _matvec(L, [Y[i][c] for i in range(m)])
        totals.append(sum(g[i] * r[i] for i in range(m)))
    return totals


def _feasible(A, limit):
    """A 是否在可行域：非负、生产性、条件数不超限。返回 (bool, 原因)。"""
    if any(v < 0 for row in A for v in row):
        return False, '系数为负'
    ok, _, _, reason = _certificate(A, limit)
    return ok, reason


def _derivative(system):
    """方向导数与差分复核。返回证据dict；弹性仅在可行步长复核一致时有效。"""
    model = system.model
    A, D, g, Y, L = system.A, system.direction, system.g, system.Y, system.L
    m, k = len(A), len(model.destinations)
    names = model.destinations
    LDL = _matmul(_matmul(L, D), L)
    dF = []
    for c in range(k):
        r = _matvec(LDL, [Y[i][c] for i in range(m)])
        dF.append(sum(g[i] * r[i] for i in range(m)))
    totals = _solve_total(A, g, Y)
    # 非负性给出的精确可行区间（生产性另按步长逐一检验）
    lo = hi = None
    for i in range(m):
        for j in range(m):
            d = D[i][j]
            if d > 0:
                b = -A[i][j] / d
                lo = b if lo is None or b > lo else lo
            elif d < 0:
                b = -A[i][j] / d
                hi = b if hi is None or b < hi else hi
    steps = []
    finest = None
    for exp in range(1, 7):
        h = Fraction(1, 10 ** exp)
        entry = {'h': str(h)}
        plus_ok, plus_why = _feasible([[A[i][j] + h * D[i][j] for j in range(m)] for i in range(m)],
                                      model.condition_limit)
        minus_ok, minus_why = _feasible([[A[i][j] - h * D[i][j] for j in range(m)] for i in range(m)],
                                        model.condition_limit)
        entry['plus_feasible'] = plus_ok
        entry['minus_feasible'] = minus_ok
        if not plus_ok:
            entry['plus_block'] = plus_why
        if not minus_ok:
            entry['minus_block'] = minus_why
        quotients = None
        mode = None
        if plus_ok and minus_ok:
            fp = _solve_total([[A[i][j] + h * D[i][j] for j in range(m)] for i in range(m)], g, Y)
            fm = _solve_total([[A[i][j] - h * D[i][j] for j in range(m)] for i in range(m)], g, Y)
            quotients = [(fp[c] - fm[c]) / (2 * h) for c in range(k)]
            mode = 'central'
        elif plus_ok or minus_ok:
            h_eff = h if plus_ok else -h
            f1 = _solve_total([[A[i][j] + h_eff * D[i][j] for j in range(m)] for i in range(m)], g, Y)
            quotients = [(f1[c] - totals[c]) / h_eff for c in range(k)]
            mode = 'one_sided'
        if quotients is not None:
            entry['mode'] = mode
            entry['quotients'] = {names[c]: str(quotients[c]) for c in range(k)}
            entry['abs_error_vs_analytic'] = {
                names[c]: str(abs(quotients[c] - dF[c])) for c in range(k)}
            finest = (h, quotients, mode)
        else:
            entry['mode'] = 'infeasible'
        steps.append(entry)
    per_dest = {}
    valid = finest is not None and all(
        abs(finest[1][c] - dF[c]) <= model.tolerance for c in range(k))
    for c in range(k):
        entry = {'dF_dt': str(dF[c])}
        if valid and totals[c] != 0:
            entry['elasticity_per_unit_t'] = str(dF[c] / totals[c])
        else:
            entry['elasticity_per_unit_t'] = None
        per_dest[names[c]] = entry
    note = None
    if finest is None:
        note = '所有差分步长均跨到不可行域，不报有效弹性'
    elif not valid:
        note = '最细可行步长的差分与解析导数不一致，不报有效弹性'
    elif any(s['mode'] == 'infeasible' or not s['plus_feasible'] or not s['minus_feasible']
             for s in steps):
        note = '部分步长跨到不可行域已剔除，弹性仅由可行步长复核'
    return {
        'definition': 'A(t)=A+t·D，t=0 处方向导数；直接强度与最终需求固定。'
                      'elasticity_per_unit_t=(1/F)·dF/dt，为对扰动参数t的半弹性。',
        'per_destination': per_dest,
        'grand_total_dF_dt': str(sum(dF)),
        'feasible_interval_nonnegative': [None if lo is None else str(lo),
                                          None if hi is None else str(hi)],
        'fd_verification': {
            'steps': steps,
            'tolerance': str(model.tolerance),
            'finest_feasible_h': None if finest is None else str(finest[0]),
            'valid': valid,
            'note': note,
        },
    }


# ---------------------------------------------------------------- 汇总入口

def run_analysis(v, manifest_sha256=None):
    """完整核算。输入非法抛 MrioError（不产出分析包）；数值受阻返回 blocked 包。"""
    model = load_model(v)
    system = build_system(model)
    package = {
        'kind': 'mrio-analysis',
        'manifest_sha256': manifest_sha256,
        'disclaimer': '隐含排放核算与结构反事实结果，非因果预测，不构成政策评价或投资建议。',
        'currency_unit': model.currency_unit,
        'sector_mapping': system.mapping,
        'accounting': {
            'rule': '每个供给部门 rowsum(Z)+rowsum(Y)==x（精确有理数判定）',
            'discrepancies': {model.sectors[i]: str(model.discrepancies[i]) for i in range(len(model.sectors))},
        },
        'solver': 'exact-rational(Fraction)',
    }
    ok, L, evidence, reason = _certificate(system.A, model.condition_limit)
    evidence['A'] = [[str(v) for v in row] for row in system.A]
    evidence['condition_limit'] = str(model.condition_limit)
    evidence['tolerance'] = str(model.tolerance)
    package['solve_evidence'] = evidence
    if not ok:
        package['status'] = 'blocked'
        package['blocks'] = [reason]
        package['responsibilities'] = None
        package['scenario'] = None
        package['derivative'] = None
        return package
    system.L = L
    per = _footprints(L, system.g, system.Y, model.destinations, system.kept_ids)
    grand = sum(Fraction(per[d]['total']) for d in model.destinations)
    emitted = sum(model.f[i] for i in system.kept)
    package['status'] = 'ok'
    package['blocks'] = []
    package['responsibilities'] = {
        'direct_intensity': {system.kept_ids[i]: str(system.g[i]) for i in range(len(system.kept))},
        'per_destination': per,
        'layers': _layers(system.A, system.g, system.Y, model.destinations, per),
        'grand_total': str(grand),
        'note': '总足迹 g·L·Y 已含全部层级；直接+间接为互补分解；层级序列为增量，'
                '不可把各层总乘数重复累加（重复归算）。',
        'emissions_conservation': {
            'grand_total': str(grand),
            'sum_direct_emissions_kept': str(emitted),
            'consistent': grand == emitted,
        },
    }
    package['scenario'] = _scenario(system, model.condition_limit) if system.cells else None
    package['derivative'] = _derivative(system)
    return package


# ---------------------------------------------------------------- 拆分与汇回

def _to_strings(M):
    return [[str(v) for v in row] for row in M]


def split_sector(v, sector_index, share):
    """把部门拆成两个同比例子部门（相同列技术、相同直接强度）。

    share 为子部门1的产出份额，须在 (0,1) 内。返回新的原始资料dict，
    可重新进入 load_model/run_analysis。汇回后足迹应精确不变。
    """
    model = load_model(v)
    n, k = len(model.sectors), len(model.destinations)
    if type(sector_index) is not int or not (0 <= sector_index < n):
        raise MrioError('split_sector: 部门索引越界')
    alpha = _rational(share, 'share')
    if not (0 < alpha < 1):
        raise MrioError('split_sector: 份额须在 (0,1) 内')
    beta = 1 - alpha
    s = sector_index
    new_sectors = model.sectors[:s] + [model.sectors[s] + '#1', model.sectors[s] + '#2'] + model.sectors[s + 1:]
    idx = list(range(s)) + [s, s] + list(range(s + 1, n))  # 新行/列 → 原行/列
    frac = [Fraction(1)] * s + [alpha, beta] + [Fraction(1)] * (n - s - 1)
    nn = n + 1
    # 行、列同份额拆分（保持两个子部门列技术与直接强度相同）；内部 2×2 块按行份额×列份额
    Z2 = []
    for i in range(nn):
        row = []
        for j in range(nn):
            z = model.Z[idx[i]][idx[j]]
            if idx[i] == s and idx[j] == s:
                z *= frac[i] * frac[j]
            elif idx[i] == s:
                z *= frac[i]
            elif idx[j] == s:
                z *= frac[j]
            row.append(z)
        Z2.append(row)
    Y2 = [[model.Y[idx[i]][c] * frac[i] for c in range(k)] for i in range(nn)]
    x2 = [model.x[idx[i]] * frac[i] for i in range(nn)]
    f2 = [model.f[idx[i]] * frac[i] for i in range(nn)]
    D2 = []
    for i in range(nn):
        row = []
        for j in range(nn):
            d = model.direction[idx[i]][idx[j]]
            if idx[i] == s:
                d *= frac[i]
            if idx[j] == s:
                d *= frac[j]
            row.append(d)
        D2.append(row)
    cell_map = {}
    for (i, j) in model.extraction_cells:
        is_ = [s, s + 1] if i == s else ([i] if i < s else [i + 1])
        js = [s, s + 1] if j == s else ([j] if j < s else [j + 1])
        for a in is_:
            for b in js:
                cell_map[(a, b)] = True
    return {
        'kind': 'mrio',
        'sectors': new_sectors,
        'destinations': model.destinations[:],
        'Z': _to_strings(Z2),
        'x': [str(q) for q in x2],
        'Y': _to_strings(Y2),
        'f': [str(q) for q in f2],
        'currency_unit': model.currency_unit,
        'extraction_cells': sorted([list(c) for c in cell_map]),
        'direction': _to_strings(D2),
        'numeric_policy': {
            'rational_or_residual_tolerance': str(model.tolerance),
            'condition_limit': str(model.condition_limit),
        },
    }


def merge_sectors(v, groups):
    """把 groups 指定的部门组汇回（行、列、x、f、Y 求和）。返回新的原始资料dict。"""
    model = load_model(v)
    n, k = len(model.sectors), len(model.destinations)
    flat = [i for g_ in groups for i in g_]
    if sorted(flat) != list(range(n)) or any(not g_ for g_ in groups):
        raise MrioError('merge_sectors: groups 必须是对全部部门的分划')
    owner = {}
    for gi, g_ in enumerate(groups):
        for i in g_:
            owner[i] = gi
    m = len(groups)
    Z2 = [[sum(model.Z[i][j] for i in groups[a] for j in groups[b]) for b in range(m)] for a in range(m)]
    Y2 = [[sum(model.Y[i][c] for i in groups[a]) for c in range(k)] for a in range(m)]
    x2 = [sum(model.x[i] for i in groups[a]) for a in range(m)]
    f2 = [sum(model.f[i] for i in groups[a]) for a in range(m)]
    D2 = [[sum(model.direction[i][j] for i in groups[a] for j in groups[b]) for b in range(m)]
          for a in range(m)]
    cells = sorted({(owner[i], owner[j]) for (i, j) in model.extraction_cells})
    return {
        'kind': 'mrio',
        'sectors': ['+'.join(model.sectors[i] for i in g_) for g_ in groups],
        'destinations': model.destinations[:],
        'Z': _to_strings(Z2),
        'x': [str(q) for q in x2],
        'Y': _to_strings(Y2),
        'f': [str(q) for q in f2],
        'currency_unit': model.currency_unit,
        'extraction_cells': [list(c) for c in cells],
        'direction': _to_strings(D2),
        'numeric_policy': {
            'rational_or_residual_tolerance': str(model.tolerance),
            'condition_limit': str(model.condition_limit),
        },
    }
