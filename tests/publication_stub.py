"""Registry boundary double for the existing isolated task/UI unit tests.

Real registry, migration and crash behavior are tested separately on temporary DBs.
"""
from src.database import database as db


class PublicationStub:
    def __init__(self,vod_id,fmt="html_online",mode=None,ref="local:chat_0123456789abcdef0123456789abcdef.html"):
        self.vod_id,self.fmt,self.ref=vod_id,fmt,ref
        self.meta,self.stats={},{}

    def reserve(self):
        pass

    def complete(self):
        return db.upsert_cache(vod_id=self.vod_id,fmt=self.fmt,vod_url=f"https://www.twitch.tv/videos/{self.vod_id}",meta=self.meta,stats=self.stats,files=[])

    def fail(self,error):
        pass

    def close(self):
        pass
