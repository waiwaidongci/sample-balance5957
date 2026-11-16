from sqlalchemy import String,JSON,ForeignKey,Integer,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class GroupBatch(Base):
    __tablename__='group_batches'
    __table_args__=(UniqueConstraint('group_id','batch_id',name='uq_group_batch'),UniqueConstraint('group_id','position',name='uq_group_position'))
    id:Mapped[int]=mapped_column(primary_key=True)
    group_id:Mapped[int]=mapped_column(ForeignKey('comparison_groups.id'))
    batch_id:Mapped[int]=mapped_column(ForeignKey('sample_batches.id'))
    position:Mapped[int]=mapped_column(Integer)
    month_label:Mapped[str]=mapped_column(String(20))
    sample_snapshot:Mapped[list]=mapped_column(JSON)
    sample_checksum:Mapped[str]=mapped_column(String(64))
    sample_epoch:Mapped[int]=mapped_column(Integer,default=1)
    status:Mapped[str]=mapped_column(String(20),default='queued')
    calibration:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    calibration_checksum:Mapped[str|None]=mapped_column(String(64),nullable=True)
    error:Mapped[str|None]=mapped_column(String(500),nullable=True)
