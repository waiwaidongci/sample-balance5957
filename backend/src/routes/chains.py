from fastapi import APIRouter,Depends
from src.config.database import session
from src.middlewares.auth import current_user,require
from src.schemas.comparison import GroupInput,PublishInput
from src.schemas.target import TargetSetInput
from src.services import chain_service
from src.controllers import chain_controller
router=APIRouter(prefix='/chains',tags=['chain'])
@router.get('')
def listing(db=Depends(session),user=Depends(current_user)):return chain_service.list_groups(db)
@router.post('',status_code=201)
def creating(data:GroupInput,db=Depends(session),user=Depends(require('admin','analyst'))):return chain_controller.create(db,data,user)
@router.get('/{group_id}')
def detail(group_id:int,db=Depends(session),user=Depends(current_user)):return chain_service.detail(db,group_id)
@router.post('/{group_id}/recompute')
def retrying(group_id:int,db=Depends(session),user=Depends(require('admin','analyst'))):return chain_controller.retry(db,group_id)
@router.post('/{group_id}/nodes/{node_id}/refresh-sample')
def refreshing(group_id:int,node_id:int,db=Depends(session),user=Depends(require('admin','analyst'))):return chain_controller.refresh_sample(db,group_id,node_id,user)
@router.put('/{group_id}/target')
def replacing(group_id:int,data:TargetSetInput,db=Depends(session),user=Depends(require('admin'))):return chain_controller.replace_target(db,group_id,data,user)
@router.post('/{group_id}/publish')
def publishing(group_id:int,data:PublishInput,db=Depends(session),user=Depends(require('admin'))):return chain_controller.publish(db,group_id,data,user)
@router.get('/{group_id}/publications')
def publications(group_id:int,db=Depends(session),user=Depends(current_user)):return chain_service.list_publications(db,group_id)
@router.get('/publications/{publication_id}')
def publication(publication_id:int,db=Depends(session),user=Depends(current_user)):return chain_service.get_publication(db,publication_id)
