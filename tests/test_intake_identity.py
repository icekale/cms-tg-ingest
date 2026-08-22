import unittest

from app.media.intake_identity import is_season_folder_name, is_video_name, snapshot_files


class IntakeIdentitySnapshotTests(unittest.TestCase):
    def test_video_suffix_and_season_names(self):
        self.assertTrue(is_video_name("拆弹专家.2017.mkv"))
        self.assertFalse(is_video_name("拆弹专家.2017.chs.ass"))
        self.assertTrue(is_season_folder_name("Season 03"))
        self.assertTrue(is_season_folder_name("第3季"))
        self.assertFalse(is_season_folder_name("C-拆弹专家-2017-[tmdb=441531]"))

    def test_snapshot_lists_videos_two_levels_for_season_roots(self):
        listed = {
            "recv-folder": [
                {"fid": "share-should-ignore", "n": "poster.jpg"},
                {"cid": "season-1", "n": "Season 1", "pid": "recv-folder"},
                {"fid": "video-root", "n": "Extra.mkv", "cid": "recv-folder"},
            ],
            "season-1": [
                {"fid": "ep1", "n": "Show.S01E01.mkv", "cid": "season-1"},
                {"fid": "sub1", "n": "Show.S01E01.ass", "cid": "season-1"},
            ],
        }

        def list_files(parent_id, limit=500):
            return list(listed.get(str(parent_id), []))

        files = snapshot_files(
            [
                {"file_id": "recv-folder", "file_name": "Show", "is_folder": True},
            ],
            list_files,
        )
        self.assertEqual(
            {(item["id"], item["name"]) for item in files},
            {("video-root", "Extra.mkv"), ("ep1", "Show.S01E01.mkv")},
        )

    def test_snapshot_single_video_root(self):
        files = snapshot_files(
            [{"file_id": "lone-mkv", "file_name": "Movie.mkv", "is_folder": False}],
            lambda *_args, **_kwargs: [],
        )
        self.assertEqual(files, [{"id": "lone-mkv", "name": "Movie.mkv"}])
