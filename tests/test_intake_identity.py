import unittest

from app.media.intake_identity import (
    cleanup_root_action,
    dest_id_from_file_hits,
    is_season_folder_name,
    is_video_name,
    snapshot_files,
)


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

    def test_snapshot_empty_folder_returns_empty_files(self):
        files = snapshot_files(
            [{"file_id": "empty-folder", "file_name": "Empty", "is_folder": True}],
            lambda *_args, **_kwargs: [],
        )
        self.assertEqual(files, [])

    def test_snapshot_raises_when_folder_list_fails(self):
        def list_files(parent_id, limit=500):
            raise RuntimeError("115 risk control")

        with self.assertRaises(RuntimeError):
            snapshot_files(
                [{"file_id": "recv-folder", "file_name": "Show", "is_folder": True}],
                list_files,
            )

    def test_snapshot_raises_when_season_list_fails(self):
        def list_files(parent_id, limit=500):
            if str(parent_id) == "recv-folder":
                return [{"cid": "season-1", "n": "Season 1", "pid": "recv-folder"}]
            raise RuntimeError("115 risk control")

        with self.assertRaises(RuntimeError):
            snapshot_files(
                [{"file_id": "recv-folder", "file_name": "Show", "is_folder": True}],
                list_files,
            )


class IntakeIdentityDestTests(unittest.TestCase):
    def test_movie_parent_is_dest(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-c-441531", "n": "C-拆弹专家-2017-[tmdb=441531]", "pid": "movie-parent"},
                {"cid": "recv-folder-402", "n": "拆弹专家 (2017) [tmdb=441531]", "pid": "redundant-cid"},
            ],
            expected_ids=["video-mkv-402"],
        )
        self.assertEqual(dest, "dest-c-441531")

    def test_season_parent_walks_up_to_show_root(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "dest-108978")

    def test_season_child_of_dest_walks_up_to_show_root(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "dest-108978")
        self.assertNotEqual(dest, "season-3")

    def test_file_parent_without_season_in_folder_hits_is_that_parent(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "season-3")

    def test_missing_file_is_incomplete(self):
        dest = dest_id_from_file_hits(
            file_hits=[],
            folder_hits=[],
            expected_ids=["video-mkv-402"],
        )
        self.assertEqual(dest, "incomplete")

    def test_two_library_roots_conflict(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-a", "cid": "dest-a", "n": "A.mkv"},
                {"fid": "ep-b", "cid": "dest-b", "n": "B.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-a", "n": "Show A", "pid": "tv-parent"},
                {"cid": "dest-b", "n": "Show B", "pid": "tv-parent"},
            ],
            expected_ids=["ep-a", "ep-b"],
        )
        self.assertEqual(dest, "conflict")


class IntakeIdentityCleanupTests(unittest.TestCase):
    def test_delete_empty_root_in_redundant(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="redundant-cid",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "delete",
        )

    def test_skip_when_root_is_dest(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="dest-c-441531",
                parent_id="movie-parent",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "skip",
        )

    def test_skip_when_root_already_gone(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid"},
            ),
            "skip",
        )

    def test_needs_action_when_root_sits_in_library(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="movie-parent",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "needs_action",
        )
