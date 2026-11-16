import random
from sqlalchemy import select,func
from src.config.database import Base,engine,SessionLocal
from src.models import User,SampleBatch,SampleUnit,MarginTarget,WeightRun,WeightReview,ComparisonGroup
from src.utils.security import hash_password
from src.services.run_service import compute
from src.schemas.comparison import GroupInput,GroupBatchInput,PublishInput
from src.services import chain_service
from src.config.queue import queue as redis_queue

def seed():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for role,name in [('admin','周衡 · 抽样负责人'),('analyst','陈野 · 交通分析师'),('viewer','陆遥 · 研究阅读者')]:
            if not db.scalar(select(User).where(User.email==role+'@balance.demo')):db.add(User(email=role+'@balance.demo',name=name,role=role,password_hash=hash_password('Balance2026!')))
        db.commit()
        for j,(code,name,n,total) in enumerate([('nov-city','11月城区出行样本',800,10000),('oct-pilot','10月试采对照',400,5000),('west-check','西区补采研究',80,1000)]):
            batch=db.scalar(select(SampleBatch).where(SampleBatch.code==code))
            if not batch:batch=SampleBatch(code=code,name=name,source_file='yy-travel-sample.csv',description='匿名合成出行样本；样本结构与目标边际不同。');db.add(batch);db.flush()
            existing=set(db.scalars(select(SampleUnit.code).where(SampleUnit.batch_id==batch.id)));rng=random.Random(73+j)
            for i in range(n):
                c=f'YY{j}-{i+1:04d}';zone=rng.choices(['中心','东区','西区'],[.6,.2,.2])[0];age=rng.choices(['青年','中年','长者'],[.6,.3,.1])[0];mode=rng.choices(['小汽车','公交','轨道'],[.6,.25,.15])[0]
                minutes=round(max(3,18+{'中心':0,'东区':8,'西区':13}[zone]+{'小汽车':0,'公交':12,'轨道':7}[mode]+rng.gauss(0,5)),2)
                if c not in existing:db.add(SampleUnit(batch_id=batch.id,code=c,zone=zone,age_band=age,mode=mode,trip_minutes=minutes,base_weight=1))
            config={'zone':{'中心':.3,'东区':.45,'西区':.25},'age_band':{'青年':.35,'中年':.45,'长者':.2},'mode':{'小汽车':.3,'公交':.35,'轨道':.35}}
            for dim,margin in config.items():
                for cat,share in margin.items():
                    if not db.scalar(select(MarginTarget).where(MarginTarget.batch_id==batch.id,MarginTarget.dimension==dim,MarginTarget.category==cat)):db.add(MarginTarget(batch_id=batch.id,dimension=dim,category=cat,population=total*share))
        db.commit();user=db.scalar(select(User).where(User.role=='admin'))
        name='城区样本代表性 · 初始校准'
        run=db.scalar(select(WeightRun).where(WeightRun.name==name))
        if not run:
            batch=db.scalar(select(SampleBatch).where(SampleBatch.code=='nov-city'))
            run=WeightRun(name=name,batch_id=batch.id,created_by=user.id,tolerance=.00001,max_iterations=500,cap_ratio=12);db.add(run);db.commit();compute(db,run.id)
            if run.result['converged']:db.add(WeightReview(run_id=run.id,decision='approved',note='合成样本演示：边际已收敛，保留有效样本与方法边界。',reviewer_id=user.id));db.commit()
        counts={name:db.scalar(select(func.count()).select_from(model)) for name,model in [('users',User),('batches',SampleBatch),('units',SampleUnit),('targets',MarginTarget),('runs',WeightRun),('reviews',WeightReview)]}
        chain_name='城区逐月 · 链式比较（冻结共同口径）'
        if not db.scalar(select(ComparisonGroup).where(ComparisonGroup.name==chain_name)):
            chain_batches=db.scalars(select(SampleBatch).where(SampleBatch.code.in_(['oct-pilot','nov-city','west-check'])).order_by(SampleBatch.id)).all()
            labels={1:'10月',2:'11月',3:'12月'}
            payload=GroupInput(name=chain_name,description='冻结共同目标与样本版本的逐月加权时长链式比较；更正只重算受影响链段。',tolerance=.0001,max_iterations=500,cap_ratio=12,batches=[GroupBatchInput(batch_id=b.id,month_label=labels[i+1]) for i,b in enumerate(chain_batches)])
            try:
                detail=chain_service.create_group(db,payload,user,redis_queue)
                chain_service.compute_group(db,detail['id'],detail['version'])
                ready=chain_service.detail(db,detail['id'])
                if ready['status']=='ready':
                    chain_service.publish(db,detail['id'],'演示种子：整组链段已收敛并通过分解对账，原子发布首版。',user)
            except Exception:
                db.rollback()
        counts['chain_groups']=db.scalar(select(func.count()).select_from(ComparisonGroup))
        print(counts);return counts
if __name__=='__main__':seed()
