"""仅核对输入形状，不生成分析结果。"""
from .io import unique,integer
def summarize(v,root):
    unique([{'id':x} for x in v['sectors']],'id');n=len(v['sectors']);k=len(v['destinations'])
    if len(v['Z'])!=n or any(len(r)!=n for r in v['Z']) or len(v['Y'])!=n or any(len(r)!=k for r in v['Y']):raise ValueError('矩阵形状错误')
    return {'sectors':n,'destinations':k,'extraction_cells':len(v['extraction_cells'])}
