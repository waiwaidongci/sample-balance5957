"""相邻月份加权均值的对称分解（Kitagawa/Oaxaca-Blinder 账户恒等式）。

格键 = (zone, age_band, mode)；格内权重为共同目标下的 raking 结果。
恒等式：总变化 ≡ 样本构成效应 + 时长效应（新增/消失格整体计入构成效应）。
本模块为纯函数，不接触数据库；结果确定性可复算。
"""
DIMS=('zone','age_band','mode')
METHOD_VERSION='kitagawa-symmetric-v1'
RECONCILE_TOLERANCE=1e-8

def cell_key(unit):
    return tuple(str(unit[d]) for d in DIMS)

def aggregate_cells(assignments):
    """按格汇总权重份额、格内加权平均时长与样本数。"""
    cells={}
    total=0.0
    for unit in assignments:
        key=cell_key(unit)
        weight=float(unit['weight'])
        if weight<=0:continue
        cell=cells.setdefault(key,{'weight':0.0,'weighted_minutes':0.0,'count':0})
        cell['weight']+=weight
        cell['weighted_minutes']+=weight*float(unit['trip_minutes'])
        cell['count']+=1
        total+=weight
    out={}
    for key,cell in cells.items():
        out[key]={'zone':key[0],'age_band':key[1],'mode':key[2],'weight':cell['weight'],'share':cell['weight']/total if total>0 else 0.0,'mean_minutes':cell['weighted_minutes']/cell['weight'],'count':cell['count']}
    return out,total

def decompose(from_calibration,to_calibration):
    cells0,_=aggregate_cells(from_calibration['assignments'])
    cells1,_=aggregate_cells(to_calibration['assignments'])
    keys=sorted(set(cells0)|set(cells1))
    composition=0.0
    duration=0.0
    rows=[]
    coverage={'common':0,'new':0,'lost':0}
    for key in keys:
        a=cells0.get(key)
        b=cells1.get(key)
        p0=a['share'] if a else 0.0
        p1=b['share'] if b else 0.0
        y0=a['mean_minutes'] if a else None
        y1=b['mean_minutes'] if b else None
        if a and b:
            comp=0.5*(p1-p0)*(y0+y1)
            dur=0.5*(p0+p1)*(y1-y0)
            tag='common'
        elif b:
            comp=p1*y1
            dur=0.0
            tag='new'
        else:
            comp=-p0*y0
            dur=0.0
            tag='lost'
        composition+=comp
        duration+=dur
        coverage[tag]+=1
        rows.append({'zone':key[0],'age_band':key[1],'mode':key[2],'coverage':tag,'share_from':p0,'share_to':p1,'mean_minutes_from':y0,'mean_minutes_to':y1,'weight_from':a['weight'] if a else 0.0,'weight_to':b['weight'] if b else 0.0,'count_from':a['count'] if a else 0,'count_to':b['count'] if b else 0,'composition_effect':comp,'duration_effect':dur})
    mean_from=float(from_calibration['weighted_mean'])
    mean_to=float(to_calibration['weighted_mean'])
    total_change=mean_to-mean_from
    residual=total_change-(composition+duration)
    return {'method':METHOD_VERSION,'weighted_mean_from':mean_from,'weighted_mean_to':mean_to,'total_change':total_change,'composition_effect':composition,'duration_effect':duration,'reconciliation_residual':residual,'reconciled':abs(residual)<=RECONCILE_TOLERANCE,'coverage':coverage,'cells':rows,'sample_count_from':from_calibration['sample_count'],'sample_count_to':to_calibration['sample_count'],'ess_from':from_calibration['effective_sample_size'],'ess_to':to_calibration['effective_sample_size'],'converged_from':bool(from_calibration['converged']),'converged_to':bool(to_calibration['converged'])}
