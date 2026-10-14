from __future__ import annotations

from pathlib import Path

import pytest

from pikiosk.catalog import Catalog, CatalogError, parse_ref, parse_time

HOSTS = ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"]


def catalog(tmp_path: Path, allow: bool = True) -> Catalog:
    videos = tmp_path / "Videos"
    videos.mkdir()
    (videos / "calm.mp4").write_bytes(b"x")
    return Catalog(
        [
            {"name": "Baby Shark", "type": "video", "id": "XqZsoesa55w"},
            {"name": "Mix", "type": "playlist", "id": "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
        ],
        allowed_hosts=HOSTS,
        allow_unlisted=allow,
        file_roots=[videos],
    )


@pytest.mark.parametrize(
    ("text", "kind", "ident", "start"),
    [
        ("XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://www.youtube.com/watch?v=XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://www.youtube.com/watch?v=XqZsoesa55w&t=90", "video", "XqZsoesa55w", 90),
        ("https://www.youtube.com/watch?v=XqZsoesa55w&t=90s", "video", "XqZsoesa55w", 90),
        ("https://www.youtube.com/watch?v=XqZsoesa55w&t=1m30s", "video", "XqZsoesa55w", 90),
        ("https://youtu.be/XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://youtu.be/XqZsoesa55w?t=12", "video", "XqZsoesa55w", 12),
        ("https://www.youtube.com/shorts/XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://www.youtube.com/embed/XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://www.youtube.com/live/XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://music.youtube.com/watch?v=XqZsoesa55w", "video", "XqZsoesa55w", None),
        ("https://m.youtube.com/watch?v=XqZsoesa55w", "video", "XqZsoesa55w", None),
        (
            "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "playlist",
            "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            None,
        ),
        (
            "https://www.youtube.com/watch?v=XqZsoesa55w&list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx&si=abc",
            "video",
            "XqZsoesa55w",
            None,
        ),
        ("https://www.youtube.com/watch?v=XqZsoesa55w&pp=1&feature=share", "video", "XqZsoesa55w", None),
        ("PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "playlist", "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx", None),
        ("https://www.youtube.com/watch?v=XqZsoesa55w&start=15", "video", "XqZsoesa55w", 15),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1h2m3s", "video", "dQw4w9WgXcQ", 3723),
    ],
)
def test_parse_table(text: str, kind: str, ident: str, start: float | None) -> None:
    ref = parse_ref(text)
    assert ref.kind == kind
    assert ref.id == ident
    assert ref.start == start


def test_time_forms() -> None:
    assert parse_time("90") == 90
    assert parse_time("90s") == 90
    assert parse_time("1m30s") == 90


def test_catalog_name(tmp_path: Path) -> None:
    ref = catalog(tmp_path).parse("Baby Shark")
    assert ref.kind == "catalog"
    assert ref.name == "Baby Shark"


def test_search_rejected(tmp_path: Path) -> None:
    with pytest.raises(CatalogError) as raised:
        catalog(tmp_path).parse("ytsearch:baby shark")
    assert raised.value.code == "not_allowed"


def test_ytdl_scheme_rejected(tmp_path: Path) -> None:
    with pytest.raises(CatalogError):
        catalog(tmp_path).parse("ytdl://ytsearch:nope")


def test_bad_host(tmp_path: Path) -> None:
    with pytest.raises(CatalogError) as raised:
        catalog(tmp_path).parse("https://example.com/watch?v=XqZsoesa55w")
    assert raised.value.code == "not_allowed"


def test_file_inside(tmp_path: Path) -> None:
    path = tmp_path / "Videos" / "calm.mp4"
    ref = catalog(tmp_path).parse(f"file:{path}")
    assert ref.kind == "file"


def test_file_outside(tmp_path: Path) -> None:
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"no")
    with pytest.raises(CatalogError) as raised:
        catalog(tmp_path).parse(f"file:{outside}")
    assert raised.value.code == "not_allowed"


def test_unlisted_blocked(tmp_path: Path) -> None:
    with pytest.raises(CatalogError) as raised:
        catalog(tmp_path, allow=False).resolve("dQw4w9WgXcQ")
    assert raised.value.code == "not_allowed"


def test_unlisted_allowed(tmp_path: Path) -> None:
    playable = catalog(tmp_path, allow=True).resolve("dQw4w9WgXcQ")
    assert playable.video_id == "dQw4w9WgXcQ"
    assert playable.source == "adhoc"


def test_strips_list_from_watch(tmp_path: Path) -> None:
    playable = catalog(tmp_path).resolve(
        "https://www.youtube.com/watch?v=XqZsoesa55w&list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx&si=zzz"
    )
    assert playable.kind if False else playable.video_id == "XqZsoesa55w"
    assert playable.playlist_id is None
