"""链式比较的确定性测试：SQLite 内存库 + 假队列，无需 MySQL/Redis。

覆盖：共同口径校准、对称分解恒等式（独立 oracle）、链段增量重算与结转、
失败恢复等价一次完成、版本隔离（并发更正拒绝/陈旧任务丢弃）、原子发布与发布保留、
样本更正只影响相邻链段、目标更正全链重算。
运行：PYTHONPATH=/workspace/backend python -m tests.test_chain
"""
import sys,types
from datetime import datetime
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

def _setup_path():
    import os
    sys.path.insert(0,'/workspace/backend')
    os.environ.setdefault('DATABASE_URL','sqlite://')
    os.environ.setdefault('REDIS_URL','redis://localhost:6399/0')
    os.environ.setdefault('JWT_SECRET','test-secret')

class FakeQueue:
    def __init__(self):self.items=[]
    def rpush(self,key,value):self.items.append((key,value))
    def blpop(self,*_a,**_k):return None
    def ping(self):return True
    def incr(self,key):return 1
    def expire(self,*_a):return True

class _User:
    def __init__(self,uid,email):self.id=uid;self.email=email;self.role='admin'

ZONES=('中心','东区','西区');AGES=('青年','中年','长者');MODES=('小汽车','公交','轨道')
TARGET_TOTAL=1000.0
TARGETS=[{'dimension':'zone','category':z,'population':v} for z,v in zip(ZONES,(300.0,450.0,250.0))]+[{'dimension':'age_band','category':a,'population':v} for a,v in zip(AGES,(350.0,450.0,200.0))]+[{'dimension':'mode','category':m,'population':v} for m,v in zip(MODES,(300.0,350.0,350.0))]

def make_units(batch_index,minutes_map=None,n_per_cell=3):
    units=[];code_i=1
    for z in ZONES:
        for a in AGES:
            for m in MODES:
                for k in range(n_per_cell):
                    base=20+ZONES.index(z)*4+MODES.index(m)*6+AGES.index(a)*2
                    if minutes_map and (z,a,m) in minutes_map:base=minutes_map[(z,a,m)]
                    units.append({'code':f'B{batch_index}-{code_i:03d}','zone':z,'age_band':a,'mode':m,'trip_minutes':float(base)+(0.1*k),'base_weight':1.0})
                    code_i+=1
    return units

class ChainHarness:
    def __init__(self):
        _setup_path()
        from src.config import database
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        database.engine=self.engine
        database.SessionLocal=sessionmaker(bind=self.engine,expire_on_commit=False)
        from src.config import queue as queue_mod
        self.fake=FakeQueue();queue_mod.queue=self.fake
        from src.models import User,SampleBatch,SampleUnit,MarginTarget
        from src.models import ComparisonGroup,GroupBatch,ChainSegment,ChainPublication,Audit
        from src.config.database import Base
        Base.metadata.create_all(self.engine)
        self.db=database.SessionLocal()
        self.user=User(email='admin@balance.demo',name='负责人',role='admin',password_hash='x');self.db.add(self.user);self.db.commit()
        self.batches=[]
        for i in range(3):
            batch=SampleBatch(code=f'month-{i+1:02d}',name=f'{i+1}月批次',source_file='synth.csv',description='')
            self.db.add(batch);self.db.flush()
            for unit in make_units(i+1):self.db.add(SampleUnit(batch_id=batch.id,**unit))
            for target in TARGETS:self.db.add(MarginTarget(batch_id=batch.id,**target))
            self.batches.append(batch)
        self.db.commit()
        from src.services import chain_service
        from src.services import decomposition
        from src.schemas.comparison import GroupInput,GroupBatchInput,PublishInput
        from src.schemas.target import TargetSetInput,TargetInput
        self.cs=chain_service;self.decomp=decomposition
        self.GroupInput=GroupInput;self.GroupBatchInput=GroupBatchInput;self.PublishInput=PublishInput
        self.TargetSetInput=TargetSetInput;self.TargetInput=TargetInput
        self.ComparisonGroup=ComparisonGroup;self.GroupBatch=GroupBatch;self.ChainSegment=ChainSegment;self.ChainPublication=ChainPublication
        self.db.expire_all()

    def group_input(self,name='城区逐月链式比较',labels=('09月','10月','11月')):
        return self.GroupInput(name=name,batches=[self.GroupBatchInput(batch_id=b.id,month_label=label) for b,label in zip(self.batches,labels)],tolerance=.0001,max_iterations=500,cap_ratio=12)

    def drain(self):
        jobs=list(self.fake.items);self.fake.items.clear()
        for key,payload in jobs:
            gid,ver=payload.split(':');self.cs.compute_group(self.db,int(gid),int(ver))

    def create_and_run(self,**kwargs):
        data=self.group_input(**kwargs)
        detail=self.cs.create_group(self.db,data,self.user,self.fake)
        assert detail['version']==1 and detail['status']=='recomputing'
        assert len(self.fake.items)==1
        self.drain()
        return self.cs.detail(self.db,detail['id'])

    def close(self):self.db.close()

def test_decomposition_oracle():
    h=ChainHarness()
    try:
        from src.services.raking_algorithm import calibrate
        # 独立构造两月：同一格结构，时长与份额已知，手算 oracle
        units0=make_units(1,n_per_cell=2);units1=make_units(2,minutes_map={(z,a,m):40.0 for z in ZONES for a in AGES for m in MODES},n_per_cell=2)
        r0=calibrate(units0,TARGETS,.0001,500,12);r1=calibrate(units1,TARGETS,.0001,500,12)
        result=h.decomp.decompose(r0,r1)
        assert result['reconciled'],result['reconciliation_residual']
        assert abs(result['reconciliation_residual'])<1e-12
        # 份额结构完全相同（同格、同 n、同目标）=> 构成效应应为零
        assert abs(result['composition_effect'])<1e-8,result['composition_effect']
        assert abs(result['duration_effect']-result['total_change'])<1e-8
        # 直接按份额×时长重算 oracle
        cells0,_=h.decomp.aggregate_cells(r0['assignments']);cells1,_=h.decomp.aggregate_cells(r1['assignments'])
        oracle_comp=sum(0.5*(cells1[k]['share']-cells0[k]['share'])*(cells0[k]['mean_minutes']+cells1[k]['mean_minutes']) for k in cells0)
        oracle_dur=sum(0.5*(cells0[k]['share']+cells1[k]['share'])*(cells1[k]['mean_minutes']-cells0[k]['mean_minutes']) for k in cells0)
        assert abs(oracle_comp-result['composition_effect'])<1e-12
        assert abs(oracle_dur-result['duration_effect'])<1e-12
        assert result['coverage']['common']==27 and result['coverage']['new']==0 and result['coverage']['lost']==0
    finally:h.close()

def test_full_chain_and_reconciliation():
    h=ChainHarness()
    try:
        detail=h.create_and_run()
        assert detail['status']=='ready'
        nodes=detail['nodes'];segments=[s for s in detail['segments'] if s['version']==1]
        assert len(nodes)==3 and len(segments)==2
        assert all(n['status']=='completed' and n['calibration']['converged'] for n in nodes)
        assert all(s['status']=='completed' and s['result']['reconciled'] for s in segments)
        for s in segments:
            r=s['result']
            assert abs((r['total_change']-(r['composition_effect']+r['duration_effect']))-r['reconciliation_residual'])<1e-12
            assert r['weighted_mean_to']-r['weighted_mean_from']==r['total_change']
        # 全部节点的校准输入 checksum 必须与当前冻结口径一致
        for n in nodes:
            assert n['calibration_checksum']==h.cs._expected_calibration_checksum(h.db.get(h.GroupBatch,n['id']),h.db.get(h.ComparisonGroup,detail['id']))
    finally:h.close()

def test_incremental_sample_correction():
    h=ChainHarness()
    try:
        detail=h.create_and_run();gid=detail['id']
        nodes=detail['nodes']
        before={s['position']:s['input_checksum'] for s in detail['segments'] if s['version']==1}
        mid_node=h.db.get(h.GroupBatch,nodes[1]['id'])
        # 更正中间月份样本：只新增 1 个观测（新格），只应影响相邻两条链段
        from src.models import SampleUnit
        h.db.add(SampleUnit(batch_id=mid_node.batch_id,code='B2-NEW1',zone='中心',age_band='青年',mode='小汽车',trip_minutes=33.0,base_weight=1.0));h.db.commit()
        h.cs.refresh_sample(h.db,gid,mid_node.id,h.user,h.fake)
        assert h.fake.items and h.fake.items[0][0]==h.cs.CHAIN_QUEUE
        v2=h.cs.detail(h.db,gid);assert v2['version']==2 and v2['status']=='recomputing'
        h.drain()
        v2=h.cs.detail(h.db,gid);assert v2['status']=='ready'
        seg1=[s for s in v2['segments'] if s['version']==1];seg2=[s for s in v2['segments'] if s['version']==2]
        assert len(seg1)==2 and len(seg2)==2
        # 未受影响的两端节点校准 checksum 不变
        refreshed={n['position']:n for n in v2['nodes']}
        assert refreshed[0]['calibration_checksum']==nodes[0]['calibration_checksum']
        assert refreshed[2]['calibration_checksum']==nodes[2]['calibration_checksum']
        assert refreshed[1]['calibration_checksum']!=nodes[1]['calibration_checksum']
        # 链段0/1 都与中间月份相邻，输入 checksum 均应改变；分解仍恒等对账
        r0=next(s for s in seg2 if s['position']==0)
        r1=next(s for s in seg2 if s['position']==1)
        assert r0['input_checksum']!=before[0] and r1['input_checksum']!=before[1]
        assert r0['result']['reconciled'] and r1['result']['reconciled']
        assert r0['result']['coverage']==r1['result']['coverage']=={'common':27,'new':0,'lost':0}
        # 版本1链段原样保留
        assert all(s['status']=='completed' for s in seg1)
        assert before
    finally:h.close()

def test_carryforward_equivalence():
    h=ChainHarness()
    try:
        detail=h.create_and_run();gid=detail['id']
        first_checksums={s['position']:s['input_checksum'] for s in detail['segments'] if s['version']==1}
        # 仅更正最后一个月：第一条链段输入不变，应从 v1 结转且结果逐字节相同
        from src.models import SampleUnit
        last=h.db.get(h.GroupBatch,detail['nodes'][-1]['id'])
        h.db.add(SampleUnit(batch_id=last.batch_id,code='B3-NEW1',zone='东区',age_band='长者',mode='公交',trip_minutes=50.0,base_weight=1.0));h.db.commit()
        h.cs.refresh_sample(h.db,gid,last.id,h.user,h.fake);h.drain()
        v2=h.cs.detail(h.db,gid)
        seg0_v1=next(s for s in v2['segments'] if s['position']==0 and s['version']==1)
        seg0_v2=next(s for s in v2['segments'] if s['position']==0 and s['version']==2)
        assert seg0_v2['status']=='completed'
        assert seg0_v2['input_checksum']==seg0_v1['input_checksum']==first_checksums[0]
        assert seg0_v2['result']==seg0_v1['result']
    finally:h.close()

def test_failure_recovery_equivalent():
    h=ChainHarness()
    try:
        detail=h.create_and_run()
        # 全量重算参照：用新目标触发全链，完整执行一次
        gid=detail['id']
        targets=[h.TargetInput(**t) for t in TARGETS]
        for t in targets:
            if t.dimension=='zone' and t.category=='中心':t.population=330.0
            if t.dimension=='zone' and t.category=='东区':t.population=420.0
        h.cs.replace_target(h.db,gid,h.TargetSetInput(targets=targets),h.user,h.fake)
        job=list(h.fake.items)[-1];h.fake.items.clear()
        g,ver=job[1].split(':');g=int(g);ver=int(ver)
        # 模拟中途崩溃：每个节点后强行中断两次，再完整恢复
        import src.services.chain_service as mod
        original=mod.calibrate
        calls={'n':0}
        def flaky(units,tg,tol,it,cap):
            calls['n']+=1
            if calls['n'] in (2,4):raise RuntimeError('模拟worker崩溃')
            return original(units,tg,tol,it,cap)
        mod.calibrate=flaky
        try:
            for _attempt in range(8):
                # 与 src.worker 一致：任务失败落库后，通过 retry 重新入队同版本任务
                h.cs.compute_group(h.db,g,ver)
                group=h.db.get(h.ComparisonGroup,g)
                if group.status=='ready':break
                h.cs.retry_recompute(h.db,g,h.fake)
                assert h.fake.items[-1]==(h.cs.CHAIN_QUEUE,f'{g}:{ver}')
                h.fake.items.clear()
        finally:mod.calibrate=original
        recovered=h.cs.detail(h.db,g)
        assert recovered['status']=='ready' and recovered['version']==2
        # 参照：另建同结构组，一次完成
        ref=h.create_and_run(name='参照一次完成组',labels=('09月','10月','11月'))
        ref_gid=ref['id']
        h.cs.replace_target(h.db,ref_gid,h.TargetSetInput(targets=targets),h.user,h.fake);h.drain()
        ref=h.cs.detail(h.db,ref_gid)
        got=[s['result'] for s in recovered['segments'] if s['version']==2]
        want=[s['result'] for s in ref['segments'] if s['version']==2]
        assert got==want
        assert [n['calibration_checksum'] for n in recovered['nodes']]==[n['calibration_checksum'] for n in ref['nodes']]
    finally:h.close()

def test_version_guard_and_stale_job():
    h=ChainHarness()
    try:
        detail=h.create_and_run();gid=detail['id']
        from src.models import SampleUnit
        node=h.db.get(h.GroupBatch,detail['nodes'][0]['id'])
        h.db.add(SampleUnit(batch_id=node.batch_id,code='B1-NEW1',zone='西区',age_band='中年',mode='轨道',trip_minutes=60.0,base_weight=1.0));h.db.commit()
        h.cs.refresh_sample(h.db,gid,node.id,h.user,h.fake)
        # 重算进行中：更正被 409 拒绝
        from fastapi import HTTPException
        node=h.db.get(h.GroupBatch,node.id)
        h.db.add(SampleUnit(batch_id=node.batch_id,code='B1-NEW2',zone='西区',age_band='中年',mode='轨道',trip_minutes=61.0,base_weight=1.0));h.db.commit()
        try:
            h.cs.refresh_sample(h.db,gid,node.id,h.user,h.fake);raise AssertionError('应拒绝并发更正')
        except HTTPException as exc:assert exc.status_code==409
        # 陈旧版本任务（v1）必须被丢弃，不写坏 v2
        h.cs.compute_group(h.db,gid,1)
        mid=h.cs.detail(h.db,gid);assert mid['status']=='recomputing' and mid['version']==2
        h.drain()
        done=h.cs.detail(h.db,gid);assert done['status']=='ready'
        # 再次投递陈旧任务仍是空操作
        h.cs.compute_group(h.db,gid,1)
        assert h.cs.detail(h.db,gid)['status']=='ready'
    finally:h.close()

def test_atomic_publish_and_retention():
    h=ChainHarness()
    try:
        detail=h.create_and_run();gid=detail['id']
        from fastapi import HTTPException
        # 未就绪不能发布
        h.db.get(h.ComparisonGroup,gid).status='recomputing';h.db.commit()
        try:
            h.cs.publish(h.db,gid,'整组冻结口径发布',h.user);raise AssertionError('应拒绝未就绪发布')
        except HTTPException as exc:assert exc.status_code==409
        h.db.get(h.ComparisonGroup,gid).status='ready';h.db.commit()
        pub=h.cs.publish(h.db,gid,'整组冻结口径发布 v1',h.user)
        assert pub['version']==1 and len(pub['checksum'])==64
        group=h.db.get(h.ComparisonGroup,gid);assert group.published_version==1
        # 同版本重复发布幂等
        again=h.cs.publish(h.db,gid,'重复提交',h.user);assert again['id']==pub['id']
        # v2 重算期间，v1 发布行完整保留可读
        from src.models import SampleUnit
        node=h.db.get(h.GroupBatch,detail['nodes'][0]['id'])
        h.db.add(SampleUnit(batch_id=node.batch_id,code='B1-PUB1',zone='中心',age_band='长者',mode='公交',trip_minutes=44.0,base_weight=1.0));h.db.commit()
        h.cs.refresh_sample(h.db,gid,node.id,h.user,h.fake)
        assert h.cs.detail(h.db,gid)['status']=='recomputing'
        kept=h.cs.get_publication(h.db,pub['id'])
        assert kept['version']==1 and kept['snapshot']['segments']
        h.drain()
        pub2=h.cs.publish(h.db,gid,'整组冻结口径发布 v2',h.user)
        assert pub2['version']==2 and pub2['checksum']!=pub['checksum']
        pubs=h.cs.list_publications(h.db,gid);assert len(pubs)==2
    finally:h.close()

def test_target_correction_recomputes_all():
    h=ChainHarness()
    try:
        detail=h.create_and_run();gid=detail['id']
        old=[n['calibration_checksum'] for n in detail['nodes']]
        targets=[h.TargetInput(**t) for t in TARGETS]
        for t in targets:
            if t.dimension=='mode' and t.category=='轨道':t.population=300.0
            if t.dimension=='mode' and t.category=='小汽车':t.population=350.0
        h.cs.replace_target(h.db,gid,h.TargetSetInput(targets=targets),h.user,h.fake)
        assert h.db.get(h.ComparisonGroup,gid).target_epoch==2
        h.drain()
        v2=h.cs.detail(h.db,gid);assert v2['status']=='ready'
        new=[n['calibration_checksum'] for n in v2['nodes']]
        assert all(a!=b for a,b in zip(old,new))
        assert all(s['version']==2 and s['status']=='completed' for s in v2['segments'] if s['version']==2)
        # 无变化的目标更正被拒绝
        from fastapi import HTTPException
        try:
            h.cs.replace_target(h.db,gid,h.TargetSetInput(targets=targets),h.user,h.fake);raise AssertionError('应拒绝无变化更正')
        except HTTPException as exc:assert exc.status_code==409
    finally:h.close()

def test_new_and_lost_cells_count_as_composition():
    h=ChainHarness()
    try:
        from src.services.raking_algorithm import calibrate
        # 月份1保留两个格；月份2只保留其中一个并新增一个格 → 同时出现 common/new/lost
        # 每月都覆盖全部边际类别，但三维交叉格部分不同 → 同时出现 common/new/lost
        cells=[('中心','青年','小汽车',25.0),('东区','中年','公交',40.0),('西区','长者','轨道',48.0),('东区','青年','轨道',33.0)]
        units0=[{'code':f'A{i}','zone':z,'age_band':a,'mode':m,'trip_minutes':y,'base_weight':1.0} for i,(z,a,m,y) in enumerate(cells*4)]
        cells1=[('中心','青年','小汽车',28.0),('西区','中年','小汽车',44.0),('东区','长者','公交',50.0),('中心','长者','轨道',55.0)]
        units1=[{'code':f'C{i}','zone':z,'age_band':a,'mode':m,'trip_minutes':y,'base_weight':1.0} for i,(z,a,m,y) in enumerate(cells1*4)]
        # 共同目标：每月样本都支持全部目标类别（新增/消失发生在三维交叉格，而非边际类别）
        cats={'zone':['中心','东区','西区'],'age_band':['青年','中年','长者'],'mode':['小汽车','公交','轨道']}
        targets=[{'dimension':d,'category':c,'population':900.0/len(cs)} for d,cs in cats.items() for c in cs]
        r0=calibrate(units0,targets,.0001,500,12);r1=calibrate(units1,targets,.0001,500,12)
        result=h.decomp.decompose(r0,r1)
        assert result['coverage']=={'common':1,'new':3,'lost':3}
        assert result['reconciled']
        new_row=next(c for c in result['cells'] if c['coverage']=='new')
        lost_row=next(c for c in result['cells'] if c['coverage']=='lost')
        assert new_row['duration_effect']==0.0 and lost_row['duration_effect']==0.0
        assert new_row['composition_effect']>0 and lost_row['composition_effect']<0
    finally:h.close()

def test_worker_crash_marking_and_retry():
    h=ChainHarness()
    try:
        detail=h.create_and_run()
        from src.models import SampleUnit
        from src.worker import mark_chain_failure
        node=h.db.get(h.GroupBatch,detail['nodes'][0]['id'])
        h.db.add(SampleUnit(batch_id=node.batch_id,code='B1-CRASH1',zone='中心',age_band='中年',mode='公交',trip_minutes=47.0,base_weight=1.0));h.db.commit()
        h.cs.refresh_sample(h.db,detail['id'],node.id,h.user,h.fake)
        gid=detail['id'];version=h.cs.detail(h.db,gid)['version']
        # 模拟进程在节点 running 时被杀死：残留 running 行由 worker 兜底落库
        running=h.db.scalars(select(h.GroupBatch).where(h.GroupBatch.group_id==gid,h.GroupBatch.status=='queued')).first()
        running.status='running';h.db.commit()
        mark_chain_failure(h.db,gid,version,RuntimeError('进程崩溃'))
        failed=h.cs.detail(h.db,gid)
        assert failed['status']=='failed'
        assert any(n['status']=='failed' for n in failed['nodes'])
        # 同版本重投后恢复，结果与正常一次完成一致
        h.cs.retry_recompute(h.db,gid,h.fake);h.drain()
        recovered=h.cs.detail(h.db,gid);assert recovered['status']=='ready' and recovered['version']==version
        assert all(n['status']=='completed' for n in recovered['nodes'])
        assert all(s['status']=='completed' and s['result']['reconciled'] for s in recovered['segments'] if s['version']==version)
    finally:h.close()

def main():
    tests=[(name,obj) for name,obj in sorted(globals().items()) if name.startswith('test_') and callable(obj)]
    for name,fn in tests:
        fn();print(f'PASS {name}')
    print(f'\n{len(tests)} chain tests passed')

if __name__=='__main__':main()
