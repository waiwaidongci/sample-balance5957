from pydantic import BaseModel,Field
class GroupBatchInput(BaseModel):
    batch_id:int=Field(gt=0)
    month_label:str=Field(min_length=1,max_length=20)
class GroupInput(BaseModel):
    name:str=Field(min_length=2,max_length=100)
    description:str=Field(default='',max_length=500)
    tolerance:float=Field(default=.0001,ge=.000001,le=.05,allow_inf_nan=False)
    max_iterations:int=Field(default=500,ge=1,le=2000)
    cap_ratio:float=Field(default=12,ge=1,le=30,allow_inf_nan=False)
    batches:list[GroupBatchInput]=Field(min_length=2,max_length=12)
class PublishInput(BaseModel):
    note:str=Field(min_length=4,max_length=500)
