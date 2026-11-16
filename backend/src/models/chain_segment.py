from sqlalchemy import String,JSON,ForeignKey,Integer,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class ChainSegment(Base):
    __tablename__='chain_segments'
    __table_args__=(UniqueConstraint('group_id','position','version',name='uq_segment_version'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    group_id:Mapped[int]=mapped_column(ForeignKey('comparison_groups.id'))
    from_node_id:Mapped[int]=mapped_column(ForeignKey('group_batches.id'))
    to_node_id:Mapped[int]=mapped_column(ForeignKey('group_batches.id'))
    position:Mapped[int]=mapped_column(Integer)
    version:Mapped[int]=mapped_column(Integer)
    status:Mapped[str]=mapped_column(String(20),default='queued')
    input_checksum:Mapped[str]=mapped_column(String(64))
    result:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str|None]=mapped_column(String(500),nullable=True)
