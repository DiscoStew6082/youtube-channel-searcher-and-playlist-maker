import pytest

from app.youtube import ChannelRef, parse_channel_ref


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UCabc12345678901234567890", ChannelRef("id", "UCabc12345678901234567890")),
        ("@naturechannel", ChannelRef("handle", "@naturechannel")),
        ("https://www.youtube.com/@naturechannel/videos", ChannelRef("handle", "@naturechannel")),
        (
            "https://www.youtube.com/channel/UCabc12345678901234567890",
            ChannelRef("id", "UCabc12345678901234567890"),
        ),
        ("https://www.youtube.com/c/NatureChannel", ChannelRef("query", "NatureChannel")),
        ("youtube.com/user/NatureChannel", ChannelRef("query", "NatureChannel")),
    ],
)
def test_parse_channel_ref(raw, expected):
    assert parse_channel_ref(raw) == expected


def test_parse_channel_ref_rejects_non_youtube_url():
    with pytest.raises(ValueError):
        parse_channel_ref("https://example.com/@naturechannel")
