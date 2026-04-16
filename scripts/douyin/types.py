"""抖音数据类型定义。"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Author:
    uid: str = ""
    sec_uid: str = ""
    nickname: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Author":
        return cls(
            uid=d.get("uid", ""),
            sec_uid=d.get("secUid", d.get("sec_uid", "")),
            nickname=d.get("nickname", ""),
        )


@dataclass
class VideoStats:
    digg_count: int = 0
    comment_count: int = 0
    play_count: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "VideoStats":
        return cls(
            digg_count=d.get("diggCount", d.get("digg_count", 0)),
            comment_count=d.get("commentCount", d.get("comment_count", 0)),
            play_count=d.get("playCount", d.get("play_count", 0)),
        )


@dataclass
class Video:
    aweme_id: str = ""
    desc: str = ""
    create_time: int = 0
    is_top: bool = False
    author: Author = field(default_factory=Author)
    stats: VideoStats = field(default_factory=VideoStats)

    @classmethod
    def from_dict(cls, d: dict) -> "Video":
        return cls(
            aweme_id=d.get("awemeId", d.get("aweme_id", "")),
            desc=d.get("desc", ""),
            create_time=d.get("createTime", d.get("create_time", 0)),
            is_top=bool(d.get("is_top", 0)),
            author=Author.from_dict(d.get("author", d.get("authorInfo", {}))),
            stats=VideoStats.from_dict(d.get("stats", d.get("statistics", {}))),
        )
