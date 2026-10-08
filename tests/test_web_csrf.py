"""The dashboard refuses form posts that come from another site."""
from app.web import same_origin

HOST = "100.64.0.1:8420"


def test_same_site_post_is_allowed():
    assert same_origin(HOST, "http://100.64.0.1:8420", None)
    assert same_origin(HOST, None, "http://100.64.0.1:8420/")


def test_other_site_is_refused():
    assert not same_origin(HOST, "http://evil.example", None)
    assert not same_origin(HOST, "null", None)
    assert not same_origin(HOST, None, "https://evil.example/page")
    # A matching Referer does not rescue a foreign Origin.
    assert not same_origin(HOST, "http://evil.example", "http://100.64.0.1:8420/")


def test_no_headers_means_not_a_browser_form():
    assert same_origin(HOST, None, None)
