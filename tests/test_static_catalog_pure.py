import json

from catalog import refresh
from static_catalog import generate


def test_static_catalog_generates_relative_media_links(tmp_path):
    data_root = tmp_path / "data"
    media = data_root / "media-strict" / "channel" / "aaaaaaaaaaa"
    media.mkdir(parents=True)
    (media / "channel.aaaaaaaaaaa.20260101.Title [aaaaaaaaaaa].out.info.json").write_text(
        json.dumps({"id": "aaaaaaaaaaa", "title": "Title", "uploader": "Artist"}),
        encoding="utf-8",
    )
    (media / "Title [aaaaaaaaaaa].out.mp4").write_bytes(b"video")
    (media / "Title [aaaaaaaaaaa].out.m4a").write_bytes(b"audio")
    database = data_root / "media-catalog.db"
    refresh(media.parent.parent, database)

    output = generate(data_root)
    html = (output / "index.html").read_text(encoding="utf-8")
    assert "../media-strict/channel/aaaaaaaaaaa/" in html
    assert (output / "app.js").is_file()
    assert (output / "styles.css").is_file()
