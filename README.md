# 跨区域投入产出的隐含排放与提取反事实

本项目保存气候行动研究的合成资料与领域口径，并提供多区域投入产出(MRIO)核算工具。`analysis_data.io`读取JSON及核验原始文件身份，`analysis_data.domain`核对基本形状，`analysis_data.mrio`执行精确有理数核算，`analysis_data.cli`提供命令行入口。详细量纲、时间、契约和统计口径见[资料说明](docs/data-contract.md)。地区、家庭、合同、价格及排放数据均为合成，不代表真实政策评价或投资建议。

Python 3.11以上，无第三方依赖。项目根目录可运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q analysis_data tests
python -m analysis_data.cli inspect data/normal.json   # 只清点资料，不做分析
python -m analysis_data.cli run data/normal.json -o out/normal-analysis.json
python -m analysis_data.cli demo                        # 演示三类情形的结论
```

## 核算口径

- `run` 先校验：Z、Y、f、x 为精确非负有理量（缺项不等于0），每个供给部门满足 rowsum(Z)+rowsum(Y)==x；零产出部门仅在交易、最终需求、排放全零时剔除并保留映射，否则拒绝（不除零、不以小常数填补）。
- 构造 A=Z·diag(x)⁻¹、g=f/x，以精确有理数求 L=(I−A)⁻¹。生产性以 Hawkins-Simon 主元全正且 L 非负为证；条件数（1-范数，精确）超过 `numeric_policy.condition_limit` 或非生产性时，正式结论被阻止：分析包 `status=blocked`，责任/情景/导数为 null，只保留证据。会计平衡通过不等于可解。
- 责任按目的地给出 g_i·(L·Y)_ik、总量及直接/间接互补分解；层级序列是增量而非总乘数，不可逐层重复累加。各目的地合计精确等于保留部门直接排放合计（排放守恒，无重复归算）。
- 提取情景把 `extraction_cells` 登记的 A 单元置零，其余 A、g、Y 固定，重新解产出与足迹。这是假设投入被无排放替代的结构反事实，不是因果预测，也不要求情景满足原会计恒等式。
- 方向导数按 A(t)=A+t·D 在 t=0 取解析导数 dF/dt=g·L·D·L·Y，并以步长递减（1/10…1/10⁶）的精确差分复核；跨到不可行域（负系数、非生产、条件数超限）的步长被剔除，全部步长不可行时不报有效弹性。
- `split_sector`/`merge_sectors` 支持把一个部门拆成两个同比例子部门再汇回；拆分前后各目的地足迹精确不变（见测试）。

## 退出码

`run`：0 结论有效；1 输入非法（不写出任何分析包）；2 结论被阻止（写出 `status=blocked` 的证据包）。非法输入不会留下看似成功的分析包。

Dockerfile固定Linux镜像；`docker build -t climate-analysis .`构建，`docker run --rm climate-analysis`显示摘要，`docker run --rm climate-analysis python -m unittest discover -s tests -v`运行检查，`docker run --rm climate-analysis python -m analysis_data.cli demo`查看演示。资料均可本地读取，运行不需要线上账户。
