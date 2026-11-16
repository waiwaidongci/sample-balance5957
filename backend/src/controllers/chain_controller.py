from fastapi import HTTPException
from src.config.queue import queue
from src.schemas.target import TargetSetInput
from src.services import chain_service

def create(db,data,user):
    try:
        return chain_service.create_group(db,data,user,queue)
    except HTTPException:
        raise
    except Exception:
        db.rollback();raise HTTPException(503,'任务服务暂不可用，请稍后重试')

def refresh_sample(db,group_id,node_id,user):
    return chain_service.refresh_sample(db,group_id,node_id,user,queue)

def replace_target(db,group_id,data:TargetSetInput,user):
    return chain_service.replace_target(db,group_id,data,user,queue)

def retry(db,group_id):
    return chain_service.retry_recompute(db,group_id,queue)

def publish(db,group_id,data,user):
    # 审计在 service 单事务内落库，保证与发布原子一致
    return chain_service.publish(db,group_id,data.note,user)
