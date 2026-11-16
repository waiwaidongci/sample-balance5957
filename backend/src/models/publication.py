from datetime import datetime
from sqlalchemy import String,JSON,ForeignKey,DateTime,Integer,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class ChainPublication(Base):
    __tablename__='chain_publications'
    __table_args__=(UniqueConstraint('group_id','version',name='uq_publication_version'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    group_id:Mapped[int]=mapped_column(ForeignKey('comparison_groups.id'))
    version:Mapped[int]=mapped_column(Integer)
    note:Mapped[str]=mapped_column(String(500))
    snapshot:Mapped[dict]=mapped_column(JSON)
    checksum:Mapped[str]=mapped_column(String(64))
    published_by:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
