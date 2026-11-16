"""链式比较编排：冻结快照、按链段增量重算、版本隔离与原子发布。

所有写操作在同一把组级行锁（SELECT ... FOR UPDATE）内规划；
worker 任务携带 expected_version，版本不符直接丢弃，杜绝并发更正与重算混用版本。
确定性 raking + 分解使崩溃重试与一次完成的结果逐字节一致。
"""
from fastapi import HTTPException
from sqlalchemy import select,func
from src.models import ComparisonGroup,GroupBatch,ChainSegment,ChainPublication,Audit
from src.services.raking_algorithm import calibrate,validate_targets
from src.services.decomposition import decompose,METHOD_VERSION
from src.utils.checksums import sha256_json
from src.utils.serializers import serialize
from .batch_service import get_batch
from .unit_service import list_units
from .target_service import list_targets
CHAIN_QUEUE='balance:chain'
CALIB_KEYS=('converged','iterations','max_residual','target_total','weight_total','sample_count','effective_sample_size','design_effect','weight_min','weight_max','weight_cap','unweighted_mean','weighted_mean','diagnostics','assignments','checksum','method_note')
UNIT_KEYS=('code','zone','age_band','mode','trip_minutes','base_weight')

def get_group(db,group_id):
    row=db.get(ComparisonGroup,group_id)
    if not row:raise HTTPException(404,'链式比较组不存在')
    return row

def list_groups(db):
    out=[]
    for group in db.scalars(select(ComparisonGroup).order_by(ComparisonGroup.id.desc())):
        data=serialize(group)
        data['node_count']=db.scalar(select(func.count()).select_from(GroupBatch).where(GroupBatch.group_id==group.id))
        data['segment_count']=db.scalar(select(func.count()).select_from(ChainSegment).where(ChainSegment.group_id==group.id,ChainSegment.version==group.version))
        data['publication_count']=db.scalar(select(func.count()).select_from(ChainPublication).where(ChainPublication.group_id==group.id))
        out.append(data)
    return out

def _snapshot_units(db,batch_id):
    units=[{k:u[k] for k in UNIT_KEYS} for u in list_units(db,batch_id)]
    return units,sha256_json(units)

def _snapshot_targets(db,batch_id):
    targets=[{k:t[k] for k in ('dimension','category','population')} for t in list_targets(db,batch_id)]
    targets=sorted(targets,key=lambda t:(t['dimension'],t['category']))
    return targets,sha256_json(targets)

def _expected_calibration_checksum(node,group):
    payload={'units':node.sample_snapshot,'targets':group.target_snapshot,'tolerance':group.tolerance,'max_iterations':group.max_iterations,'cap_ratio':group.cap_ratio}
    return sha256_json(payload)

def _segment_input_checksum(from_checksum,to_checksum):
    return sha256_json({'from':from_checksum,'to':to_checksum,'method':METHOD_VERSION})

def _nodes(db,group_id):
    return list(db.scalars(select(GroupBatch).where(GroupBatch.group_id==group_id).order_by(GroupBatch.position)))

def create_group(db,data,user,queue):
    if db.scalar(select(ComparisonGroup).where(ComparisonGroup.name==data.name)):raise HTTPException(409,'链式比较组名称已存在')
    entries=data.batches
    if len({e.batch_id for e in entries})!=len(entries):raise HTTPException(422,'同一批次不能在链中重复出现')
    if len({e.month_label for e in entries})!=len(entries):raise HTTPException(422,'月份标签不能重复')
    first=get_batch(db,entries[0].batch_id)
    targets,target_checksum=_snapshot_targets(db,first.id)
    if not targets:raise HTTPException(422,'锚点批次尚未登记目标边际，无法冻结共同目标')
    nodes=[]
    for position,entry in enumerate(entries):
        batch=get_batch(db,entry.batch_id)
        units,sample_checksum=_snapshot_units(db,batch.id)
        try:validate_targets(units,targets)
        except ValueError as exc:raise HTTPException(422,f'{batch.name} 与共同目标不兼容：{exc}')
        nodes.append((batch,entry.month_label,units,sample_checksum))
    group=ComparisonGroup(name=data.name,description=data.description,tolerance=data.tolerance,max_iterations=data.max_iterations,cap_ratio=data.cap_ratio,target_snapshot=targets,target_checksum=target_checksum,target_epoch=1,version=0,status='draft',created_by=user.id)
    db.add(group);db.flush()
    for position,(batch,label,units,checksum) in enumerate(nodes):
        db.add(GroupBatch(group_id=group.id,batch_id=batch.id,position=position,month_label=label,sample_snapshot=units,sample_checksum=checksum,sample_epoch=1,status='queued'))
    db.add(Audit(actor=user.email,action='create_chain',target=group.name))
    db.commit()
    _plan_and_enqueue(db,group.id,queue)
    return detail(db,group.id)

def _plan_locked(db,group):
    """调用方须已持有组行 FOR UPDATE 锁且事务未提交：版本+1、建新链段。"""
    group.version+=1
    group.status='recomputing';group.last_error=None
    nodes=_nodes(db,group.id)
    for position,(a,b) in enumerate(zip(nodes,nodes[1:])):
        db.add(ChainSegment(group_id=group.id,from_node_id=a.id,to_node_id=b.id,position=position,version=group.version,status='queued',input_checksum=''))
    return group.version

def _enqueue(db,group,queue):
    try:queue.rpush(CHAIN_QUEUE,f'{group.id}:{group.version}')
    except Exception:
        group.status='failed';group.last_error='队列不可用，请重试该链段重算';db.commit()
        raise HTTPException(503,'任务服务暂不可用，请稍后重试')

def _plan_and_enqueue(db,group_id,queue):
    group=db.scalar(select(ComparisonGroup).where(ComparisonGroup.id==group_id).with_for_update())
    _plan_locked(db,group);db.commit()
    _enqueue(db,group,queue)

def _guard_editable(db,group_id):
    group=db.scalar(select(ComparisonGroup).where(ComparisonGroup.id==group_id).with_for_update())
    if not group:raise HTTPException(404,'链式比较组不存在')
    if group.status=='recomputing':raise HTTPException(409,'链段正在重算：更正与重算不能混用版本，请等待当前版本完成后再更正')
    return group

def refresh_sample(db,group_id,node_id,user,queue):
    group=_guard_editable(db,group_id)
    node=db.get(GroupBatch,node_id)
    if not node or node.group_id!=group_id:raise HTTPException(404,'链节点不存在')
    units,checksum=_snapshot_units(db,node.batch_id)
    if checksum==node.sample_checksum:raise HTTPException(409,'该批次样本相对冻结快照没有更正')
    try:validate_targets(units,group.target_snapshot)
    except ValueError as exc:raise HTTPException(422,f'更正后样本与共同目标不兼容：{exc}')
    node.sample_snapshot=units;node.sample_checksum=checksum;node.sample_epoch+=1
    node.status='queued';node.calibration=None;node.calibration_checksum=None;node.error=None
    db.add(Audit(actor=user.email,action='correct_chain_sample',target=f'{group_id}:{node_id}'))
    version=_plan_locked(db,group);db.commit()
    _enqueue(db,group,queue)
    return detail(db,group_id)

def replace_target(db,group_id,target_set,user,queue):
    group=_guard_editable(db,group_id)
    targets=sorted((t.model_dump() for t in target_set.targets),key=lambda t:(t['dimension'],t['category']))
    for node in _nodes(db,group_id):
        try:validate_targets(node.sample_snapshot,targets)
        except ValueError as exc:raise HTTPException(422,f'{node.month_label} 样本与新目标不兼容：{exc}')
    checksum=sha256_json(targets)
    if checksum==group.target_checksum:raise HTTPException(409,'共同目标相对冻结快照没有更正')
    group.target_snapshot=targets;group.target_checksum=checksum;group.target_epoch+=1
    for node in _nodes(db,group_id):
        node.status='queued';node.calibration=None;node.calibration_checksum=None;node.error=None
    db.add(Audit(actor=user.email,action='correct_chain_target',target=str(group_id)))
    _plan_locked(db,group);db.commit()
    _enqueue(db,group,queue)
    return detail(db,group_id)

def retry_recompute(db,group_id,queue):
    """失败/中断后的同版本重试：不推进版本、不重建链段，仅重新入队当前版本。"""
    group=db.scalar(select(ComparisonGroup).where(ComparisonGroup.id==group_id).with_for_update())
    if not group:raise HTTPException(404,'链式比较组不存在')
    if group.status=='ready':return detail(db,group_id)
    if group.status not in ('failed','recomputing'):raise HTTPException(409,'当前链状态不允许重试')
    try:queue.rpush(CHAIN_QUEUE,f'{group_id}:{group.version}')
    except Exception:
        group.status='failed';group.last_error='队列不可用，请稍后重试';db.commit();raise HTTPException(503,'任务服务暂不可用，请稍后重试')
    return detail(db,group_id)

def compute_group(db,group_id,expected_version,queue=None):
    """worker 入口：只处理匹配版本的重算任务；逐节点/链段提交，已完成部分跳过。

    组状态为 recomputing（正常重算）或 failed（崩溃后重试入队）都允许进入；
    其他状态或版本不符的陈旧任务直接丢弃，保证不混用版本。
    """
    group=db.scalar(select(ComparisonGroup).where(ComparisonGroup.id==group_id).with_for_update())
    if not group or group.version!=expected_version or group.status not in ('recomputing','failed'):return
    group.status='recomputing';group.last_error=None;db.commit()
    nodes=_nodes(db,group_id)
    for node in nodes:
        if node.status=='running':node.status='queued';node.error=None
        if node.status=='completed' and node.calibration_checksum==_expected_calibration_checksum(node,group):continue
        node.status='running';db.commit()
        try:
            result=calibrate(node.sample_snapshot,group.target_snapshot,group.tolerance,group.max_iterations,group.cap_ratio)
        except Exception as exc:
            db.rollback();node=db.get(GroupBatch,node.id);node.status='failed';node.error=str(exc)[:500]
            group=db.get(ComparisonGroup,group_id);group.status='failed';group.last_error=f'{node.month_label} 校准失败：{str(exc)[:300]}';db.commit();return
        node.calibration={k:result[k] for k in CALIB_KEYS}
        node.calibration_checksum=result['checksum'];node.status='completed';node.error=None;db.commit()
    segments=list(db.scalars(select(ChainSegment).where(ChainSegment.group_id==group_id,ChainSegment.version==expected_version).order_by(ChainSegment.position)))
    for segment in segments:
        a=db.get(GroupBatch,segment.from_node_id);b=db.get(GroupBatch,segment.to_node_id)
        checksum=_segment_input_checksum(a.calibration_checksum,b.calibration_checksum)
        if segment.status=='completed' and segment.input_checksum==checksum:continue
        prior=db.scalar(select(ChainSegment).where(ChainSegment.group_id==group_id,ChainSegment.position==segment.position,ChainSegment.version<expected_version,ChainSegment.status=='completed',ChainSegment.input_checksum==checksum).order_by(ChainSegment.version.desc()))
        segment.status='running';segment.input_checksum=checksum;db.commit()
        if prior:
            segment.result=prior.result;segment.status='completed';segment.error=None;db.commit();continue
        try:result=decompose(a.calibration,b.calibration)
        except Exception as exc:
            db.rollback();segment=db.get(ChainSegment,segment.id);segment.status='failed';segment.error=str(exc)[:500]
            group=db.get(ComparisonGroup,group_id);group.status='failed';group.last_error=f'{a.month_label}→{b.month_label} 分解失败：{str(exc)[:300]}';db.commit();return
        if not result['reconciled']:
            segment.status='failed';segment.error='分解对账残差超差，未通过恒等式校验';segment.result=result;db.commit()
            group=db.get(ComparisonGroup,group_id);group.status='failed';group.last_error=f'{a.month_label}→{b.month_label} 分解对账失败';db.commit();return
        segment.result=result;segment.status='completed';segment.error=None;db.commit()
    group.status='ready';group.last_error=None;db.commit()

def publish(db,group_id,note,user):
    group=db.scalar(select(ComparisonGroup).where(ComparisonGroup.id==group_id).with_for_update())
    if not group:raise HTTPException(404,'链式比较组不存在')
    if group.status!='ready':raise HTTPException(409,'整组链段尚未全部计算完成，不能发布；已发布版本仍可查阅')
    existing=db.scalar(select(ChainPublication).where(ChainPublication.group_id==group_id,ChainPublication.version==group.version))
    if existing:return serialize(existing)
    nodes=_nodes(db,group_id)
    if any(not (node.calibration or {}).get('converged') for node in nodes):raise HTTPException(409,'存在未收敛月份，整组结果不能发布')
    segments=list(db.scalars(select(ChainSegment).where(ChainSegment.group_id==group_id,ChainSegment.version==group.version).order_by(ChainSegment.position)))
    snapshot={'group':serialize(group),'target':{'snapshot':group.target_snapshot,'checksum':group.target_checksum,'epoch':group.target_epoch},'nodes':[serialize(n) for n in nodes],'segments':[serialize(s) for s in segments]}
    row=ChainPublication(group_id=group_id,version=group.version,note=note,snapshot=snapshot,checksum=sha256_json(snapshot),published_by=user.id)
    db.add(row);db.flush();group.published_version=group.version
    db.add(Audit(actor=user.email,action='publish_chain',target=f'{group_id}:{group.version}'))
    db.commit();return serialize(row)

def list_publications(db,group_id):
    get_group(db,group_id)
    return [serialize(x) for x in db.scalars(select(ChainPublication).where(ChainPublication.group_id==group_id).order_by(ChainPublication.id.desc()))]

def get_publication(db,publication_id):
    row=db.get(ChainPublication,publication_id)
    if not row:raise HTTPException(404,'发布记录不存在')
    return serialize(row)

def detail(db,group_id):
    group=get_group(db,group_id)
    data=serialize(group)
    nodes=[]
    for node in _nodes(db,group_id):
        item=serialize(node);item['batch']=serialize(get_batch(db,node.batch_id))
        item['depends_on']={'sample':{'batch_id':node.batch_id,'epoch':node.sample_epoch,'checksum':node.sample_checksum},'target':{'epoch':group.target_epoch,'checksum':group.target_checksum},'params':{'tolerance':group.tolerance,'max_iterations':group.max_iterations,'cap_ratio':group.cap_ratio},'calibration_checksum':node.calibration_checksum}
        nodes.append(item)
    segment_rows=list(db.scalars(select(ChainSegment).where(ChainSegment.group_id==group_id).order_by(ChainSegment.version,ChainSegment.position)))
    segments=[]
    for row in segment_rows:
        item=serialize(row)
        a=db.get(GroupBatch,row.from_node_id);b=db.get(GroupBatch,row.to_node_id)
        item['from_label']=a.month_label;item['to_label']=b.month_label
        segments.append(item)
    data['nodes']=nodes;data['segments']=segments
    data['publications']=list_publications(db,group_id)
    return data
