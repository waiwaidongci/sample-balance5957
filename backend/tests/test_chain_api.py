"""链式比较的 HTTP 端到端测试：真实 FastAPI 路由 + SQLite + 假 Redis，覆盖权限、审计与序列化。"""
import sys,os
sys.path.insert(0,'/workspace/backend')
os.environ.setdefault('DATABASE_URL','sqlite://')
os.environ.setdefault('REDIS_URL','redis://localhost:6399/0')
os.environ.setdefault('JWT_SECRET','test-secret')

from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

class FakeRedis:
    def __init__(self):self.chain=[]
    def rpush(self,k,v):self.chain.append((k,v))
    def blpop(self,*a,**k):return None
    def ping(self):return True
    def incr(self,k):return 1
    def expire(self,k,s):return True

def main():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    from src.config import database,queue as queue_mod
    database.engine=engine;database.SessionLocal=Session
    fake=FakeRedis();queue_mod.queue=fake
    from src.config.database import Base
    import src.models
    Base.metadata.create_all(engine)
    from src.main import app
    from src.models import User,SampleBatch,SampleUnit,MarginTarget,Audit
    from src.utils.security import hash_password
    with Session() as db:
        admin=User(email='admin@balance.demo',name='负责人',role='admin',password_hash=hash_password('pw'))
        viewer=User(email='viewer@balance.demo',name='阅读者',role='viewer',password_hash=hash_password('pw'))
        db.add_all([admin,viewer]);db.commit()
        targets=[{'dimension':'zone','category':z,'population':v} for z,v in [('中心',300.0),('东区',450.0),('西区',250.0)]]+[{'dimension':'age_band','category':a,'population':v} for a,v in [('青年',350.0),('中年',450.0),('长者',200.0)]]+[{'dimension':'mode','category':m,'population':v} for m,v in [('小汽车',300.0),('公交',350.0),('轨道',350.0)]]
        batch_ids=[]
        for bi in range(2):
            b=SampleBatch(code=f'm{bi}',name=f'{bi+1}月',source_file='x.csv',description='');db.add(b);db.flush();batch_ids.append(b.id)
            i=1
            for z in ('中心','东区','西区'):
                for a in ('青年','中年','长者'):
                    for m in ('小汽车','公交','轨道'):
                        for _ in range(2):db.add(SampleUnit(batch_id=b.id,code=f'{bi}-{i:03d}',zone=z,age_band=a,mode=m,trip_minutes=20.0+bi*3+0.01*i,base_weight=1.0));i+=1
            for t in targets:db.add(MarginTarget(batch_id=b.id,**t))
        db.commit()
    client=TestClient(app)
    login=client.post('/auth/login',json={'email':'admin@balance.demo','password':'pw'})
    assert login.status_code==200,login.text
    token=login.json()['access_token'];H={'Authorization':'Bearer '+token}
    vlogin=client.post('/auth/login',json={'email':'viewer@balance.demo','password':'pw'})
    VH={'Authorization':'Bearer '+vlogin.json()['access_token']}

    # viewer 不能建组
    r=client.post('/chains',headers=VH,json={'name':'无权建组','batches':[{'batch_id':batch_ids[0],'month_label':'10月'},{'batch_id':batch_ids[1],'month_label':'11月'}]})
    assert r.status_code==403,r.text
    # 建组
    r=client.post('/chains',headers=H,json={'name':'API 端到端链','batches':[{'batch_id':batch_ids[0],'month_label':'10月'},{'batch_id':batch_ids[1],'month_label':'11月'}]})
    assert r.status_code==201,r.text
    detail=r.json();gid=detail['id'];assert detail['version']==1 and detail['status']=='recomputing'
    assert len(detail['nodes'])==2 and detail['nodes'][0]['depends_on']['target']['checksum']
    # 同步执行入队任务（假队列只记录负载）
    from src.services.chain_service import compute_group,CHAIN_QUEUE
    with Session() as db:
        for key,payload in fake.chain:
            g,v=payload.split(':');compute_group(db,int(g),int(v))
    r=client.get(f'/chains/{gid}',headers=H);assert r.status_code==200
    d=r.json();assert d['status']=='ready'
    seg=d['segments'][0];assert seg['status']=='completed' and seg['result']['reconciled']
    # 列表接口
    r=client.get('/chains',headers=H);assert r.status_code==200 and any(g['id']==gid for g in r.json())
    # 未收敛保护之外的正常发布
    r=client.post(f'/chains/{gid}/publish',headers=H,json={'note':'端到端原子发布'});assert r.status_code==200,r.text
    pub=r.json();assert pub['version']==1 and pub['snapshot']['segments']
    # viewer 不能发布
    r=client.post(f'/chains/{gid}/publish',headers=VH,json={'note':'无权'});assert r.status_code==403
    # 发布记录可读
    r=client.get(f'/chains/{gid}/publications',headers=VH);assert r.status_code==200 and len(r.json())==1
    r=client.get(f'/chains/publications/{pub["id"]}',headers=H);assert r.status_code==200 and r.json()['checksum']==pub['checksum']
    # 样本更正：先在实时批次补一条，再拉取
    with Session() as db:
        db.add(SampleUnit(batch_id=batch_ids[0],code='0-9001',zone='中心',age_band='青年',mode='小汽车',trip_minutes=33.0,base_weight=1.0));db.commit()
    r=client.post(f'/chains/{gid}/nodes/{d["nodes"][0]["id"]}/refresh-sample',headers=H)
    assert r.status_code==200,r.text
    assert r.json()['version']==2 and r.json()['status']=='recomputing'
    # 重算中再更正 → 409 版本隔离
    r=client.post(f'/chains/{gid}/nodes/{d["nodes"][0]["id"]}/refresh-sample',headers=H)
    assert r.status_code==409,r.text
    with Session() as db:
        for key,payload in fake.chain:
            g,v=payload.split(':');compute_group(db,int(g),int(v))
    r=client.get(f'/chains/{gid}',headers=H);d2=r.json();assert d2['status']=='ready' and d2['version']==2
    # 旧发布仍保留
    pubs=client.get(f'/chains/{gid}/publications',headers=H).json();assert len(pubs)==1 and pubs[0]['version']==1
    # 审计落库
    with Session() as db:
        actions={a.action for a in db.scalars(select(Audit)).all()}
    assert {'create_chain','publish_chain','correct_chain_sample'}<=actions,actions
    print('PASS http end-to-end chain flow')

if __name__=='__main__':main()
