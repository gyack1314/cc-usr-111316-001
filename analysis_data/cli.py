"""查看资料身份与数量，或运行多区域投入产出核算并保存分析包。

退出码：0 成功（含 inspect/demo）；1 输入非法（不产出分析包）；2 结论被阻止
（近奇异或非生产性，仍写出带 status=blocked 的证据包，正式结论为 null）。
"""
import argparse, json, sys
from pathlib import Path
from .io import load
from .domain import summarize
from .mrio import run_analysis
from .demo import demo


def main():
    p = argparse.ArgumentParser(prog='analysis_data.cli')
    p.add_argument('command', choices=['inspect', 'run', 'demo'])
    p.add_argument('file', nargs='?', help='资料清单 JSON（inspect/run 需要）')
    p.add_argument('-o', '--out', help='run：分析包输出路径；缺省打印到标准输出')
    a = p.parse_args()
    if a.command == 'inspect':
        if not a.file:
            p.error('inspect 需要资料文件')
        v, h = load(a.file)
        print(json.dumps({'manifest_sha256': h, 'summary': summarize(v, Path(a.file).parent)},
                         ensure_ascii=False, indent=2))
        return 0
    if a.command == 'demo':
        return demo(Path(a.file) if a.file else Path('data'))
    if not a.file:
        p.error('run 需要资料文件')
    try:
        v, h = load(a.file)
        package = run_analysis(v, manifest_sha256=h)
    except (ValueError, OSError) as exc:
        print(f'输入非法：{exc}\n未生成分析包。', file=sys.stderr)
        return 1
    text = json.dumps(package, ensure_ascii=False, indent=2)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + '\n', encoding='utf8')
        print(f'分析包已保存：{out}（status={package["status"]}）')
    else:
        print(text)
    return 0 if package['status'] == 'ok' else 2


if __name__ == '__main__':
    sys.exit(main())
