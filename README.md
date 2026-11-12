# 跨区域投入产出的隐含排放与提取反事实

本项目保存气候行动研究的合成资料与领域口径。`analysis_data.io`读取JSON及核验原始文件身份，`analysis_data.domain`核对基本形状，`analysis_data.cli`展示数量。详细量纲、时间、契约和统计口径见[资料说明](docs/data-contract.md)。地区、家庭、合同、价格及排放数据均为合成，不代表真实政策评价或投资建议。

Python 3.11以上，无第三方依赖。项目根目录可运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q analysis_data tests
python -m analysis_data.cli inspect data/normal.json
```

`inspect`只展示资料，不做拟合、求解或分析发布。`invalid-config.json`包含基本配置错误，其余清单在身份上完整，可能包含需领域分析判断的不可识别、无界、冲突或不适用情况。有理数以字符串保留，null不等于0，随机种子属于分析设计。

Dockerfile固定Linux镜像；`docker build -t climate-analysis .`构建，`docker run --rm climate-analysis`显示摘要，`docker run --rm climate-analysis python -m unittest discover -s tests -v`运行已有读取检查。资料均可本地读取，运行不需要线上账户。
