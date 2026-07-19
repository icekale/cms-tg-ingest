import unittest

from app.media.sources import parse_media_sources


ED2K = "ed2k://|file|Example.mkv|10|ABCDEF0123456789ABCDEF0123456789|/"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Example"


class MediaSourceTests(unittest.TestCase):
    def test_parse_media_sources_accepts_share_magnet_and_ed2k(self):
        sources = parse_media_sources(
            "https://115cdn.com/s/abc?password=1234\n"
            + MAGNET
            + "\n"
            + ED2K
        )

        self.assertEqual([source.source_type for source in sources], ["share", "magnet", "ed2k"])
        self.assertEqual(sources[1].source_key, "btih:0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(sources[2].source_key, "ed2k:abcdef0123456789abcdef0123456789:10")

    def test_parse_media_sources_rejects_malformed_cloud_links(self):
        self.assertEqual(parse_media_sources("magnet:?dn=no-btih ed2k://|file|bad|x|bad|/"), [])

    def test_parse_media_sources_deduplicates_same_cloud_source(self):
        link = MAGNET.upper()

        sources = parse_media_sources(link + "\n" + MAGNET)

        self.assertEqual(len(sources), 1)


if __name__ == "__main__":
    unittest.main()
