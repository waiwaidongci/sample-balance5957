import time,logging
from pathlib import Path
from sqlalchemy import select
from src.config.database import SessionLocal
from src.config.queue import queue
from src.models import WeightRun,ComparisonGroup,GroupBatch,ChainSegment
from src.services.run_service import compute,QUEUE
from src.services.chain_service import compute_group,CHAIN_QUEUE
logging.basicConfig(level=logging.INFO)
def mark_chain_failure(db,group_id,version,exc):
    """崩溃兜底：把残留 running 行落为 failed，使页面可同版本重试，不改动版本号。"""
    group=db.get(ComparisonGroup,group_id)
    if not group or group.version!=version:return
    message=str(exc)[:400]
    for node in db.scalars(select(GroupBatch).where(GroupBatch.group_id==group_id,GroupBatch.status=='running')):
        node.status='failed';node.error=message
    for segment in db.scalars(select(ChainSegment).where(ChainSegment.group_id==group_id,ChainSegment.version==version,ChainSegment.status=='running')):
        segment.status='failed';segment.error=message
    if group.status=='recomputing':
        group.status='failed';group.last_error=f'后台任务中断：{message}'
    db.commit()
def handle_item(db,name,payload):
    try:
        if name==CHAIN_QUEUE:
            group_id,expected_version=payload.split(':')
            compute_group(db,int(group_id),int(expected_version))
        else:
            compute(db,int(payload))
    except Exception as exc:
        db.rollback()
        logging.exception('background job failed: %s',payload)
        if name==CHAIN_QUEUE:
            try:mark_chain_failure(db,int(group_id),int(expected_version),exc)
            except Exception:logging.exception('chain failure marking failed')
        else:
            run=db.get(WeightRun,int(payload))
            if run:run.status='failed';run.error=str(exc)[:500];db.commit()
def main():
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=queue.blpop([CHAIN_QUEUE,QUEUE],timeout=4)
            if not item:continue
            name,payload=item
            with SessionLocal() as db:handle_item(db,name,payload)
        except Exception:logging.exception('worker loop');time.sleep(2)
if __name__=='__main__':main()
