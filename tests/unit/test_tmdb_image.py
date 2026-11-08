import pytest

from casttrail.web.routes import tmdb_image


@pytest.mark.parametrize(
    ("path", "size", "expected"),
    [
        ("/abc123.jpg", "w185", "https://image.tmdb.org/t/p/w185/abc123.jpg"),
        ("/A_b-9.png", "w92", "https://image.tmdb.org/t/p/w92/A_b-9.png"),
        ("/x.webp", "w342", "https://image.tmdb.org/t/p/w342/x.webp"),
    ],
)
def test_a_good_path_becomes_a_tmdb_image_url(
    path: str, size: str, expected: str
) -> None:
    assert tmdb_image(path, size) == expected


def test_the_default_size_is_a_poster_width() -> None:
    assert tmdb_image("/x.jpg") == "https://image.tmdb.org/t/p/w185/x.jpg"


@pytest.mark.parametrize(
    "path",
    [None, "", "x.jpg", "/x", "/x.gif", "/x.jpg/", "/a/b.jpg", "//x.jpg", "/x.jpg\n"],
)
def test_a_missing_or_odd_path_gives_no_url(path: str | None) -> None:
    assert tmdb_image(path) is None


@pytest.mark.parametrize("size", ["", "original", "w1000", "../w185", "W185"])
def test_only_known_sizes_are_allowed(size: str) -> None:
    assert tmdb_image("/x.jpg", size) is None
